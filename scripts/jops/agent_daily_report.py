#!/usr/bin/env python3
"""
Agent Performance Daily Report Generator
Queries CRM DB and writes cross-tab + detail tabs to Google Sheets.
"""
import subprocess, json, sys, datetime
from collections import defaultdict

GAPI = "/opt/jops/venv/bin/python /root/.hermes/profiles/operator/skills/productivity/google-workspace/scripts/google_api.py"
WS = "workspace_1l3urgumjmspnjxohclmfz6fx"

def run_sql(sql_file):
    """Execute SQL file against the CRM database"""
    result = subprocess.run(
        ['bash', '-c', f'docker exec -i twenty-db-1 psql -U twenty -d default -t -A -F "|" < {sql_file}'],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        print(f"SQL ERROR: {result.stderr}", file=sys.stderr)
        return []
    lines = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
    return lines

def write_sheet(sheet_id, range_name, values):
    """Write values to a Google Sheet range"""
    values_json = json.dumps(values)
    result = subprocess.run(
        ['bash', '-c', f'{GAPI} sheets update {sheet_id} "{range_name}" --values \'{values_json}\' 2>/dev/null'],
        capture_output=True, text=True, timeout=30
    )
    return result.returncode == 0

def append_sheet(sheet_id, range_name, values):
    """Append values to a Google Sheet"""
    values_json = json.dumps(values)
    result = subprocess.run(
        ['bash', '-c', f'{GAPI} sheets append {sheet_id} "{range_name}" --values \'{values_json}\' 2>/dev/null'],
        capture_output=True, text=True, timeout=30
    )
    return result.returncode == 0

# Get yesterday's date for the report
yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
report_date = datetime.date.today().strftime('%Y-%m-%d')

print(f"Generating report for date: {yesterday}")

# 1. Create the spreadsheet
result = subprocess.run(
    ['bash', '-c', f'{GAPI} sheets create --title "Agent Performance Daily Report" 2>/dev/null'],
    capture_output=True, text=True, timeout=30
)
print(f"Create result: {result.stdout}")

# Parse the spreadsheet ID
try:
    data = json.loads(result.stdout)
    sheet_id = data['spreadsheetId']
    print(f"Sheet ID: {sheet_id}")
except:
    print(f"Failed to parse sheet creation response: {result.stdout}")
    sys.exit(1)

# 2. Write the cross-tab summary (Tab 1: "Daily Report")
# Write date header and column headers
summary_data = [
    [f"Agent Performance Report — {yesterday}"],
    [],
    ["Zone", "Agent", "Unique Enquiries", "Visitors Scheduled", "Calls Made"]
]

# Read crosstab data
crosstab_lines = run_sql('/tmp/crosstab_report.sql')
for line in crosstab_lines:
    parts = line.split('|')
    if len(parts) >= 6:
        zone = parts[0].strip()
        agent = parts[1].strip()
        enquiries = parts[3].strip()
        visitors = parts[4].strip()
        calls = parts[5].strip()
        summary_data.append([zone, agent, int(enquiries), int(visitors), int(calls)])

# Add totals row
total_e = sum(r[2] for r in summary_data[3:])
total_v = sum(r[3] for r in summary_data[3:])
total_c = sum(r[4] for r in summary_data[3:])
summary_data.append(["", "TOTAL", total_e, total_v, total_c])

write_sheet(sheet_id, "Daily Report!A1:E8", summary_data)
print("✓ Tab 1 (Daily Report) written")

# 3. Write Enquiries detail (Tab 2)
enquiry_lines = run_sql('/tmp/detail_enquiries.sql')
enquiry_data = [["Zone", "Agent", "Date", "Enquiry Name", "Buyer ID", "Buyer Name", "Source"]]
for line in enquiry_lines:
    parts = line.split('|')
    if len(parts) >= 7:
        enquiry_data.append([p.strip() for p in parts[:7]])
if len(enquiry_data) == 1:
    enquiry_data.append(["", "", "No enquiries for this period", "", "", "", ""])

write_sheet(sheet_id, "Enquiries!A1:G500", enquiry_data)
print(f"✓ Tab 2 (Enquiries) written — {len(enquiry_data)-1} records")

# 4. Write Visits detail (Tab 3)
visit_lines = run_sql('/tmp/detail_visits.sql')
visit_data = [["Zone", "Agent", "Date", "Visit Name", "Buyer Profile ID", "Buyer Name", "Status", "Visit Source"]]
for line in visit_lines:
    parts = line.split('|')
    if len(parts) >= 8:
        visit_data.append([p.strip() for p in parts[:8]])
if len(visit_data) == 1:
    visit_data.append(["", "", "No visits for this period", "", "", "", "", ""])

write_sheet(sheet_id, "Visits!A1:H500", visit_data)
print(f"✓ Tab 3 (Visits) written — {len(visit_data)-1} records")

# 5. Write Calls detail (Tab 4)
call_lines = run_sql('/tmp/detail_calls.sql')
call_data = [["Zone", "Agent", "Date", "Call Name", "Type", "Direction", "Message"]]
for line in call_lines:
    parts = line.split('|')
    if len(parts) >= 7:
        call_data.append([p.strip() for p in parts[:7]])
if len(call_data) == 1:
    call_data.append(["", "", "No calls for this period", "", "", "", ""])

write_sheet(sheet_id, "Calls!A1:G500", call_data)
print(f"✓ Tab 4 (Calls) written — {len(call_data)-1} records")

print(f"\n✅ Sheet created: https://docs.google.com/spreadsheets/d/{sheet_id}")
