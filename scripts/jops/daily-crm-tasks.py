#!/usr/bin/env python3
import json
import urllib.request
import smtplib
from datetime import datetime, timezone, timedelta
from collections import defaultdict

IST = timezone(timedelta(hours=5, minutes=30))
NOW_IST = datetime.now(IST)
TODAY = NOW_IST.date()

GRAPHQL_URL = "http://localhost:3000/graphql"
API_KEY_PATH = "/root/.twenty/api_key.txt"
SMTP_HOST = "172.18.0.1"
SMTP_PORT = 1025
FROM_EMAIL = "support@jumbohomes.in"

with open(API_KEY_PATH) as f:
    API_KEY = f.read().strip()

def gql(query, variables=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=data,
        headers={
            "Authorization": "Bearer " + API_KEY,
            "Content-Type": "application/json"
        }
    )
    resp = urllib.request.urlopen(req, timeout=30)
    result = json.loads(resp.read())
    if "errors" in result:
        raise Exception("GraphQL error: " + str(result["errors"]))
    return result["data"]

TASK_QUERY = """
query GetTasks {
  tasks(first: 200, orderBy: {dueAt: AscNullsLast}) {
    edges {
      node {
        id
        title
        status
        dueAt
        assignee {
          id
          name {
            firstName
            lastName
          }
          userEmail
        }
      }
    }
  }
}
"""

print("Querying Twenty CRM for tasks...")
result = gql(TASK_QUERY)
edges = result["tasks"]["edges"]
print("Total tasks returned:", len(edges))

by_person = defaultdict(lambda: {"overdue": [], "today": []})
unassigned = {"overdue": [], "today": []}
sent_count = 0
errors_list = []

for edge in edges:
    task = edge["node"]
    status = task["status"]
    if status == "DONE":
        continue

    title = task["title"] or "(Untitled)"
    due_at_str = task.get("dueAt")

    if due_at_str:
        due_dt = datetime.fromisoformat(due_at_str.replace("Z", "+00:00"))
        due_ist = due_dt.astimezone(IST)
        due_date_str = due_ist.strftime("%b %d, %I:%M %p")
        is_overdue = due_ist < NOW_IST
        is_today = due_ist.date() == TODAY
    else:
        due_date_str = "No due date"
        is_overdue = False
        is_today = False

    if not is_overdue and not is_today:
        continue

    assignee = task.get("assignee")
    if assignee:
        email = assignee.get("userEmail")
        name_parts = assignee.get("name") or {}
        first = name_parts.get("firstName", "")
        last = name_parts.get("lastName", "")
        name = (first + " " + last).strip() or "Team Member"
    else:
        email = None
        name = "Unassigned"

    entry = {"title": title, "due_date": due_date_str}

    if is_overdue:
        if email:
            by_person[email]["overdue"].append(entry)
        else:
            unassigned["overdue"].append(entry)
    elif is_today:
        if email:
            by_person[email]["today"].append(entry)
        else:
            unassigned["today"].append(entry)

print("People with tasks:", len(by_person))
print("Unassigned overdue:", len(unassigned["overdue"]), "today:", len(unassigned["today"]))

def send_email(to_email, name, overdue_tasks, today_tasks):
    lines = []
    lines.append("Subject: Your CRM Tasks for " + NOW_IST.strftime("%A, %B %d"))
    lines.append("")
    lines.append("Hi " + name + ",")
    lines.append("")
    lines.append("Here are your tasks in the Jumbo Homes CRM:")
    lines.append("")

    if overdue_tasks:
        lines.append("--- OVERDUE (" + str(len(overdue_tasks)) + " tasks) ---")
        lines.append("")
        for i, t in enumerate(overdue_tasks, 1):
            lines.append(str(i) + ". " + t["title"] + " - Due: " + t["due_date"])
        lines.append("")

    if today_tasks:
        lines.append("--- TODAY (" + str(len(today_tasks)) + " tasks) ---")
        lines.append("")
        for i, t in enumerate(today_tasks, 1):
            lines.append(str(i) + ". " + t["title"] + " - Due: " + t["due_date"])
        lines.append("")

    lines.append("Please update the status in the CRM when done.")
    lines.append("https://admin.jumbohomes.in")
    lines.append("")
    lines.append("Thanks,")
    lines.append("Jumbo Homes Team")

    body = "\r\n".join(lines)

    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        server.sendmail(FROM_EMAIL, [to_email], body)
        server.quit()
        return True
    except Exception as e:
        errors_list.append("Failed to send to " + to_email + ": " + str(e)[:100])
        return False

for email, tasks in by_person.items():
    ok = send_email(email, email.split("@")[0].title(), tasks["overdue"], tasks["today"])
    if ok:
        sent_count += 1
        print("  Sent to", email, "(" + str(len(tasks["overdue"])) + " overdue, " + str(len(tasks["today"])) + " today)")

print("")
print("=== SUMMARY ===")
print("Emails sent:", sent_count)
print("Recipients:", ", ".join(by_person.keys()) if by_person else "None")
print("Unassigned overdue:", len(unassigned["overdue"]))
print("Unassigned today:", len(unassigned["today"]))
if errors_list:
    print("Errors:", len(errors_list))
    for e in errors_list:
        print("  ", e)
print("Done.")
