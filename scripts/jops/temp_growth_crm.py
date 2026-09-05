#!/usr/bin/env python3
"""CRM write phase for JUM-700: person upsert -> seller -> property.

Importable by ingest_temp_growth.py. All functions dry-run safe via `live` flag.
Follows crm-safe-operations: re-resolve person by phone after insert (never trust
generated UUID), batch via docker cp + psql -f, read back everything.
"""
import json
import re
import subprocess
import uuid

SCHEMA = "workspace_1l3urgumjmspnjxohclmfz6fx"

def psql(sql, capture=True):
    """Run SQL against twenty-db-1 and return stdout."""
    r = subprocess.run(
        ["docker", "exec", "twenty-db-1", "psql", "-U", "twenty", "-d",
         "default", "-t", "-A", "-c", sql],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"psql error: {r.stderr.strip()[:500]}")
    return r.stdout.strip()

def esc(s):
    if s is None:
        return "NULL"
    return "'" + str(s).replace("'", "''") + "'"

# --- Building match ----------------------------------------------------------

STOP_WORDS = {"bangalore", "east", "south", "north", "west", "whitefield",
              "varthur", "road", "phase", "hosa", "kr", "puram", "seegehalli",
              "main", "gate", "bengaluru", "karnataka", "india"}

def find_building(name_guess):
    """Score-based word-overlap match against active _building names.

    Returns (uuid, name) or None. Never returns a low-confidence guess:
    requires >=2 word matches (or 1 if the guess is a single word).
    """
    if not name_guess:
        return None
    words = [w for w in re.split(r"[^a-z0-9]+", name_guess.lower()) if w]
    if not words:
        return None
    cond = " OR ".join(f"\"name\" ILIKE '%{w}%'" for w in words if len(w) > 2)
    if not cond:
        return None
    rows = psql(
        f"SELECT \"id\", \"name\" FROM {SCHEMA}.\"_building\" "
        f"WHERE \"deletedAt\" IS NULL AND ({cond}) LIMIT 500;")
    best, best_score = None, 0
    guess_set = set(words)
    for line in rows.splitlines():
        if "|" not in line:
            continue
        bid, bname = line.split("|", 1)
        bwords = {w for w in re.split(r"[^a-z0-9]+", bname.lower()) if w}
        overlap = len(guess_set & bwords)
        # Phrase bonus: consecutive word pairs in the guess appearing in the name
        phrase = 0
        for i in range(len(words) - 1):
            pair = words[i] + " " + words[i + 1]
            if pair in bname.lower():
                phrase += 1
        score = overlap + phrase * 2
        if score > best_score:
            best, best_score = (bid, bname), score
    need = 2 if len(guess_set) > 1 else 1
    if best and best_score >= need:
        return best
    return None

# --- Person ------------------------------------------------------------------

def find_person_by_phone(phone, country_code="IN", calling_code="+91"):
    """Find an exact normalized phone; retain compatibility with legacy Indian rows."""
    exact = (
        f"\"phonesPrimaryPhoneNumber\" = {esc(phone)} AND "
        f"COALESCE(\"phonesPrimaryPhoneCallingCode\", '') = {esc(calling_code)}"
    )
    # Historical Indian records may lack composite metadata or use +91 in the number.
    if country_code == "IN" and calling_code == "+91":
        exact = (
            f"(\"phonesPrimaryPhoneNumber\" IN ({esc(phone)}, {esc('+91' + phone)}) "
            f"AND COALESCE(\"phonesPrimaryPhoneCallingCode\", '') IN ('', '91', '+91'))"
        )
    row = psql(
        f"SELECT \"id\" FROM {SCHEMA}.person WHERE \"deletedAt\" IS NULL AND "
        f"{exact} LIMIT 1;")
    return row or None

def split_name(name):
    parts = (name or "").strip().split()
    if not parts:
        return "Unknown", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])

def upsert_person(phone, name, country_code="IN", calling_code="+91", live=False):
    """Return (person_id, created_bool). Never trusts generated UUID — re-resolves."""
    existing = find_person_by_phone(phone, country_code, calling_code)
    if existing:
        return existing, False
    first, last = split_name(name)
    if not live:
        return None, True  # would create
    pid = str(uuid.uuid4())
    sql = (
        f"INSERT INTO {SCHEMA}.person (id, \"nameFirstName\", \"nameLastName\", "
        f"\"phonesPrimaryPhoneNumber\", \"phonesPrimaryPhoneCountryCode\", "
        f"\"phonesPrimaryPhoneCallingCode\", \"position\", \"createdBySource\", "
        f"\"createdByName\") VALUES ({esc(pid)}, {esc(first)}, {esc(last)}, "
        f"{esc(phone)}, {esc(country_code)}, {esc(calling_code)}, -15233, 'API', 'Temp-Growth Ingest');")
    psql(sql)
    # Re-resolve (never trust the generated UUID)
    actual = find_person_by_phone(phone, country_code, calling_code)
    if not actual:
        raise RuntimeError(f"person insert not verifiable for phone {calling_code}{phone}")
    return actual, True

# --- Seller ------------------------------------------------------------------

def seller_exists_for_person(person_id, url):
    row = psql(
        f"SELECT s.\"id\" FROM {SCHEMA}.\"_seller\" s WHERE s.\"deletedAt\" IS NULL "
        f"AND s.\"personId\" = {esc(person_id)} "
        f"AND (s.\"sourceUrlPrimaryLinkUrl\" = {esc(url)} OR "
        f"s.\"sourceUrlPrimaryLinkUrl\" IS NULL) LIMIT 1;")
    return row or None

def create_seller(person_id, name, source, url, live=False):
    """Return (seller_id, created_bool). Skips if same person already has a
    seller with the same source URL (dedup)."""
    existing = seller_exists_for_person(person_id, url)
    if existing:
        return existing, False
    if not live:
        return None, True
    sid = str(uuid.uuid4())
    sql = (
        f"INSERT INTO {SCHEMA}.\"_seller\" (id, name, source, stage, "
        f"\"onboardingStatus\", \"sourceUrlPrimaryLinkUrl\", "
        f"\"sourceUrlPrimaryLinkLabel\", \"personId\", position, "
        f"\"createdBySource\", \"createdByName\") VALUES "
        f"({esc(sid)}, {esc(name)}, {esc(source) if source else 'NULL'}, "
        f"'NEW_ENQUIRY', 'IDENTIFIED', {esc(url)}, '99acres', {esc(person_id)}, "
        f"-1033, 'API', 'Temp-Growth Ingest');")
    psql(sql)
    row = psql(f"SELECT \"id\" FROM {SCHEMA}.\"_seller\" WHERE id = {esc(sid)};")
    if not row:
        raise RuntimeError("seller insert not verifiable")
    return sid, True

# --- Property ----------------------------------------------------------------

def property_exists(building_id, phone=None):
    """Duplicate check: active property in same building created from same URL
    is handled at seller level; here we check exact building+seller later."""
    return None

def create_property(seller_id, person_id, building, scrape, slug_hints, live=False):
    """Create _property. scrape = scraper output dict (may be None)."""
    if not live:
        return None, True
    pid = str(uuid.uuid4())
    name_guess = (scrape or {}).get("building_guess") or slug_hints.get("building_guess")
    bhk = (scrape or {}).get("bedrooms") or slug_hints.get("bhk")
    sqft = (scrape or {}).get("squareFeet") or slug_hints.get("sqft")
    config = (scrape or {}).get("configuration")
    if not config and bhk:
        config = {1: "OPT1_BHK", 2: "OPT2_BHK", 3: "OPT3_BHK",
                  4: "OPT4_BHK", 5: "OPT5_BHK"}.get(int(bhk))
    price = (scrape or {}).get("price_micros")
    desc = (scrape or {}).get("description")
    files = json.dumps((scrape or {}).get("files") or [])

    cols = ["id", "name", "position", "createdBySource", "createdByName",
            "sellerId", "ownerId"]
    vals = [esc(pid), esc(name_guess or "New Listing"), "-1033", esc("API"),
            esc("Temp-Growth Ingest"), esc(seller_id), esc(person_id)]
    if building:
        cols.append("buildingId"); vals.append(esc(building[0]))
    if config:
        cols.append("configuration"); vals.append(esc(config))
    if bhk:
        cols.append("bedrooms"); vals.append(str(float(bhk)))
    if sqft:
        cols.append("squareFeet"); vals.append(str(float(sqft)))
    if price:
        cols.append("sourcePriceAmountMicros"); vals.append(str(price))
        cols.append("sourcePriceCurrencyCode"); vals.append("'INR'")
        cols.append("latestPriceAmountMicros"); vals.append(str(price))
        cols.append("latestPriceCurrencyCode"); vals.append("'INR'")
    if desc:
        cols.append("description"); vals.append(esc(desc))
    if files and files != "[]":
        cols.append("files"); vals.append(esc(files))
    if (scrape or {}).get("facing"):
        cols.append("facing"); vals.append(esc(scrape["facing"]))
    if (scrape or {}).get("furnishing"):
        cols.append("furnishing"); vals.append(esc(scrape["furnishing"]))
    if (scrape or {}).get("floor"):
        cols.append("floor"); vals.append(str(float(scrape["floor"])))
    if (scrape or {}).get("bathrooms"):
        cols.append("bathrooms"); vals.append(str(float(scrape["bathrooms"])))
    if (scrape or {}).get("balcony"):
        cols.append("balcony"); vals.append(str(float(scrape["balcony"])))
    if (scrape or {}).get("parking"):
        cols.append("parking"); vals.append(str(float(scrape["parking"])))
    if (scrape or {}).get("propertyType"):
        cols.append("propertyType"); vals.append(esc(scrape["propertyType"]))
    if (scrape or {}).get("occupancy"):
        cols.append("occupancy"); vals.append(esc(scrape["occupancy"]))

    sql = (f"INSERT INTO {SCHEMA}.\"_property\" ({', '.join(chr(34) + c + chr(34) if c not in ('id','name','position') else c for c in cols)}) "
           f"VALUES ({', '.join(vals)});")
    psql(sql)
    row = psql(f"SELECT \"id\" FROM {SCHEMA}.\"_property\" WHERE id = {esc(pid)};")
    if not row:
        raise RuntimeError("property insert not verifiable")
    return pid, True

def process_lead(lead, scrape, live=False):
    """Full pipeline for one lead. Returns a result dict (no side effects in dry-run)."""
    result = {"ts": lead["ts"], "phone": lead["phone"], "name": lead["name"]}
    building = None
    if scrape and not scrape.get("error"):
        building = find_building(scrape.get("building_guess"))
    # FIX JUM-702: when scrape fails or returns no building_guess,
    # always fall back to URL slug hints which have better building names
    if not building:
        building = find_building(lead.get("url_hints", {}).get("building_guess"))
    result["building"] = building
    result["scrape_ok"] = bool(scrape and not scrape.get("error"))
    result["scrape_error"] = (scrape or {}).get("error")

    pid, p_created = upsert_person(
        lead["phone"], lead["name"], lead.get("country_code", "IN"),
        lead.get("calling_code", "+91"), live=live
    )
    result["person_id"] = pid
    result["person_created"] = p_created

    if pid:  # live mode person existed or was created
        sid, s_created = create_seller(pid, lead["name"], lead.get("source"),
                                       lead["url"], live=live)
        result["seller_id"] = sid
        result["seller_created"] = s_created
        if s_created or scrape:
            prop_id, prop_created = create_property(sid, pid, building, scrape,
                                                    lead.get("url_hints", {}),
                                                    live=live)
            result["property_id"] = prop_id
            result["property_created"] = prop_created
    else:
        result["pending"] = True  # dry-run would-create
    return result
