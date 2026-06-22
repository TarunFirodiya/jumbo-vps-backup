#!/usr/bin/env python3
"""
WhatsApp Notification Polling Script for Bablu / Jumbo Homes.

Runs every 5 minutes via cron. Queries Twenty CRM for new events
(visits, opportunities, properties, buyers, enquiries) since the last check
and sends notifications via the Hermes gateway API.

State is tracked in /root/scripts/notification_state.json to avoid duplicates.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────

STATE_FILE = Path("/root/scripts/notification_state.json")
PROFILE = "bablu"
WHATSAPP_TARGET = "whatsapp"  # Uses home channel from bablu config
WS = "workspace_1l3urgumjmspnjxohclmfz6fx"

# Quiet hours (IST): no non-urgent group messages between 22:00 and 08:00
QUIET_START = 22
QUIET_END = 8

# Max messages per hour (rate limiting)
MAX_GROUP_MESSAGES_PER_HOUR = 10
MESSAGE_LOG_FILE = Path("/root/scripts/notification_message_log.json")

# ── Helpers ────────────────────────────────────────────────────────────────

def now_ist():
    """Return current datetime in IST."""
    utc_now = datetime.now(timezone.utc)
    ist = utc_now + timedelta(hours=5, minutes=30)
    return ist


def is_quiet_hours():
    """Check if current IST time is within quiet hours."""
    hour = now_ist().hour
    if QUIET_START <= QUIET_END:
        return QUIET_START <= hour < QUIET_END
    else:  # wraps midnight
        return hour >= QUIET_START or hour < QUIET_END


def load_json(path, default):
    """Load JSON file or return default."""
    try:
        if path.exists():
            return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return default


def save_json(path, data):
    """Save data to JSON file."""
    path.write_text(json.dumps(data, indent=2, default=str))


def query_crm(sql):
    """Run a SQL query against the Twenty CRM PostgreSQL database."""
    cmd = [
        "docker", "exec", "twenty-db-1", "sh", "-c",
        f'PGPASSWORD=*** psql -U twenty -d default -t -A -F "|" -c "{sql}"'
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"[ERROR] psql failed: {result.stderr.strip()}", file=sys.stderr)
            return []
        lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
        return lines
    except subprocess.TimeoutExpired:
        print("[ERROR] psql timed out", file=sys.stderr)
        return []
    except Exception as e:
        print(f"[ERROR] psql exception: {e}", file=sys.stderr)
        return []


def parse_psql_lines(lines, columns):
    """Parse psql -t -A -F '|' output into list of dicts."""
    rows = []
    for line in lines:
        parts = line.split("|")
        if len(parts) == len(columns):
            rows.append(dict(zip(columns, parts)))
    return rows


def format_inr(micros_str):
    """Convert micros amount to human-readable INR."""
    try:
        micros = float(micros_str)
    except (ValueError, TypeError):
        return "N/A"
    rupees = micros / 1_000_000
    if rupees >= 1_00_00_000:
        return f"₹{rupees/1_00_00_000:.2f} Cr"
    elif rupees >= 1_00_000:
        return f"₹{rupees/1_00_000:.2f} L"
    else:
        return f"₹{rupees:,.0f}"


def format_datetime(dt_str):
    """Format datetime string to readable format."""
    try:
        # Handle various datetime formats
        dt = dt_str.split(".")[0]  # Remove microseconds
        if "T" in dt:
            dt = datetime.fromisoformat(dt_str.split(".")[0])
        else:
            dt = datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S")
        dt_ist = dt + timedelta(hours=5, minutes=30)
        return dt_ist.strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return dt_str


def get_recent_messages_count():
    """Count messages sent in the last hour for rate limiting."""
    log = load_json(MESSAGE_LOG_FILE, [])
    one_hour_ago = now_ist() - timedelta(hours=1)
    recent = [m for m in log if datetime.fromisoformat(m["timestamp"]) > one_hour_ago]
    return len(recent)


def log_message(notification_type, entity_id):
    """Log a sent notification for rate limiting."""
    log = load_json(MESSAGE_LOG_FILE, [])
    log.append({
        "timestamp": now_ist().isoformat(),
        "type": notification_type,
        "entity_id": entity_id
    })
    # Keep only last 24 hours
    cutoff = now_ist() - timedelta(hours=24)
    log = [m for m in log if datetime.fromisoformat(m["timestamp"]) > cutoff]
    save_json(MESSAGE_LOG_FILE, log)


def send_via_gateway(message):
    """Send a message via the Hermes CLI (bablu send)."""
    cmd = ["/root/.local/bin/bablu", "send", "--to", WHATSAPP_TARGET, message]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return True
        print(f"[ERROR] bablu send failed (exit {result.returncode}): {result.stderr.strip()}", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("[ERROR] bablu send timed out", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[ERROR] bablu send exception: {e}", file=sys.stderr)
        return False


# ── Query Functions ────────────────────────────────────────────────────────

def get_new_visits(last_check):
    """Get visits created since last check."""
    sql = f"""
        SELECT v.id, v.name, v."scheduledAt", v.status,
               b.name AS buyer_name, p.name AS property_name
        FROM "{WS}"."_visit" v
        LEFT JOIN "{WS}"."_buyer" b ON b.id = v."buyerProfileId"
        LEFT JOIN "{WS}"."_property" p ON p.id = v."propertyId"
        WHERE v."deletedAt" IS NULL
        AND v."createdAt" > '{last_check}'
        ORDER BY v."createdAt" DESC
    """
    lines = query_crm(sql)
    return parse_psql_lines(lines, ["id", "name", "scheduledAt", "status", "buyer_name", "property_name"])


def get_new_opportunities(last_check):
    """Get opportunities created since last check."""
    sql = f"""
        SELECT o.id, o.name, o.stage, o."amountAmountMicros", p.name AS property_name
        FROM "{WS}"."opportunity" o
        LEFT JOIN "{WS}"."_property" p ON p.id = o."propertyNewId"
        WHERE o."deletedAt" IS NULL
        AND o."createdAt" > '{last_check}'
        ORDER BY o."createdAt" DESC
    """
    lines = query_crm(sql)
    return parse_psql_lines(lines, ["id", "name", "stage", "amountAmountMicros", "property_name"])


def get_new_properties(last_check):
    """Get properties created since last check."""
    sql = f"""
        SELECT prop.id, prop.name, prop.bedrooms, prop."propertyType", prop.zone, bl.name AS building_name
        FROM "{WS}"."_property" prop
        LEFT JOIN "{WS}"."_building" bl ON bl.id = prop."buildingId"
        WHERE prop."deletedAt" IS NULL
        AND prop."createdAt" > '{last_check}'
        ORDER BY prop."createdAt" DESC
    """
    lines = query_crm(sql)
    return parse_psql_lines(lines, ["id", "name", "bedrooms", "propertyType", "zone", "building_name"])


def get_new_buyers(last_check):
    """Get buyers created since last check."""
    sql = f"""
        SELECT b.id, b.name, b."leadStage", b.source
        FROM "{WS}"."_buyer" b
        WHERE b."deletedAt" IS NULL
        AND b."createdAt" > '{last_check}'
        ORDER BY b."createdAt" DESC
    """
    lines = query_crm(sql)
    return parse_psql_lines(lines, ["id", "name", "leadStage", "source"])


def get_new_enquiries(last_check):
    """Get enquiries created since last check."""
    sql = f"""
        SELECT e.id, e."enquiryNumber", e."enquiryType", e."statusDetail", e."createdAt", p.name AS person_name
        FROM "{WS}"."_enquiry" e
        LEFT JOIN "{WS}"."person" p ON p.id = e."personId"
        WHERE e."deletedAt" IS NULL
        AND e."createdAt" > '{last_check}'
        ORDER BY e."createdAt" DESC
    """
    lines = query_crm(sql)
    return parse_psql_lines(lines, ["id", "enquiryNumber", "enquiryType", "statusDetail", "createdAt", "person_name"])


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print(f"[{now_ist().strftime('%Y-%m-%d %H:%M:%S IST')}] Starting notification poll...")

    # Load state
    state = load_json(STATE_FILE, {
        "last_check": "2026-01-01T00:00:00+05:30",
        "notified_visits": [],
        "notified_opportunities": [],
        "notified_properties": [],
        "notified_buyers": [],
        "notified_enquiries": []
    })

    last_check = state["last_check"]
    quiet = is_quiet_hours()
    recent_count = get_recent_messages_count()

    if quiet:
        print("[INFO] Quiet hours active — skipping non-urgent notifications")

    if recent_count >= MAX_GROUP_MESSAGES_PER_HOUR:
        print(f"[INFO] Rate limit hit ({recent_count}/{MAX_GROUP_MESSAGES_PER_HOUR} messages in last hour) — skipping")
        # Still update last_check so we don't re-query old events
        state["last_check"] = now_ist().isoformat()
        save_json(STATE_FILE, state)
        return

    messages_sent = 0

    # ── New Opportunities (always send, even in quiet hours) ───────────
    opportunities = get_new_opportunities(last_check)
    for opp in opportunities:
        if opp["id"] in state.get("notified_opportunities", []):
            continue
        msg = (
            f"💰 New Offer Received\n"
            f"🏠 {opp['name']}\n"
            f"💵 {format_inr(opp.get('amountAmountMicros'))}\n"
            f"📊 Stage: {opp.get('stage', 'N/A')}"
        )
        if send_via_gateway(msg):
            state.setdefault("notified_opportunities", []).append(opp["id"])
            log_message("opportunity", opp["id"])
            messages_sent += 1
            print(f"[SENT] Opportunity: {opp['name']}")

    if not quiet:
        # ── New Visits ────────────────────────────────────────────────
        visits = get_new_visits(last_check)
        for visit in visits:
            if visit["id"] in state.get("notified_visits", []):
                continue
            msg = (
                f"📅 New Visit Scheduled\n"
                f"🏠 {visit.get('property_name', 'N/A')}\n"
                f"👤 {visit.get('buyer_name', 'N/A')}\n"
                f"🕐 {format_datetime(visit.get('scheduledAt', ''))}"
            )
            if send_via_gateway(msg):
                state.setdefault("notified_visits", []).append(visit["id"])
                log_message("visit", visit["id"])
                messages_sent += 1
                print(f"[SENT] Visit: {visit['name']}")

        # ── New Properties ────────────────────────────────────────────
        properties = get_new_properties(last_check)
        for prop in properties:
            if prop["id"] in state.get("notified_properties", []):
                continue
            msg = (
                f"🏠 New Property Added\n"
                f"📍 {prop['name']} — {prop.get('building_name', 'N/A')}\n"
                f"🛏️ {prop.get('bedrooms', '?')}BHK | {prop.get('propertyType', 'N/A')}\n"
                f"🗺️ Zone: {prop.get('zone', 'N/A')}"
            )
            if send_via_gateway(msg):
                state.setdefault("notified_properties", []).append(prop["id"])
                log_message("property", prop["id"])
                messages_sent += 1
                print(f"[SENT] Property: {prop['name']}")

        # ── New Buyers ───────────────────────────────────────────────
        buyers = get_new_buyers(last_check)
        for buyer in buyers:
            if buyer["id"] in state.get("notified_buyers", []):
                continue
            msg = (
                f"👤 New Buyer: {buyer['name']}\n"
                f"📌 Source: {buyer.get('source', 'N/A')}\n"
                f"📊 Stage: {buyer.get('leadStage', 'N/A')}"
            )
            if send_via_gateway(msg):
                state.setdefault("notified_buyers", []).append(buyer["id"])
                log_message("buyer", buyer["id"])
                messages_sent += 1
                print(f"[SENT] Buyer: {buyer['name']}")

        # ── New Enquiries ────────────────────────────────────────────
        enquiries = get_new_enquiries(last_check)
        for enq in enquiries:
            if enq["id"] in state.get("notified_enquiries", []):
                continue
            msg = (
                f"📩 New Enquiry: {enq.get('enquiryNumber', 'N/A')}\n"
                f"👤 {enq.get('person_name', 'N/A')}\n"
                f"📌 Type: {enq.get('enquiryType', 'N/A')}\n"
                f"📊 Status: {enq.get('statusDetail', 'N/A')}"
            )
            if send_via_gateway(msg):
                state.setdefault("notified_enquiries", []).append(enq["id"])
                log_message("enquiry", enq["id"])
                messages_sent += 1
                print(f"[SENT] Enquiry: {enq.get('enquiryNumber', 'N/A')}")

    # Update state
    state["last_check"] = now_ist().isoformat()

    # Trim notified lists to last 1000 entries to prevent unbounded growth
    for key in ["notified_visits", "notified_opportunities", "notified_properties",
                "notified_buyers", "notified_enquiries"]:
        if key in state and len(state[key]) > 1000:
            state[key] = state[key][-1000:]

    save_json(STATE_FILE, state)
    print(f"[DONE] Sent {messages_sent} notifications. Next poll in 5 minutes.")


if __name__ == "__main__":
    main()
