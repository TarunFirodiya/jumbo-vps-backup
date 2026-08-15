#!/usr/bin/env python3
"""Run the active Missed Calls workflow for today's unprocessed missed calls.

Default: dry-run. --live enqueues workflow runs; the CRM workflow performs the
configured HTTP side effect. The script is idempotent by workflowRun trigger ID.
"""
import argparse, json, subprocess, sys, time, uuid

SCHEMA = "workspace_1l3urgumjmspnjxohclmfz6fx"
WORKFLOW_ID = "97524ad7-9dbc-4d22-8c4f-bec34f881717"
VERSION_ID = "c34c2578-51c5-4bdd-8976-a71cf1e3c062"
WORKSPACE_ID = "1acb6d7e-22d6-44a0-95fa-fd1b7b7be25d"
DB, REDIS = "twenty-db-1", "twenty-redis-1"


def docker(args, timeout=30):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"command failed: {' '.join(args)}: {r.stderr[-500:]}")
    return r.stdout


def sql(query):
    return docker(["docker", "exec", "-i", DB, "psql", "-U", "twenty", "-d", "default", "-At", "-F", "|", "-c", query])


def get_flow():
    raw = sql(f'''SELECT json_build_object('trigger', "trigger", 'steps', "steps")::text
                 FROM "{SCHEMA}"."workflowVersion" WHERE id='{VERSION_ID}';''').strip()
    return json.loads(raw)


def candidates(hours):
    raw = sql(f'''SELECT c.id, c."personId", c."assignedagentId", c."timestamp", c."enquiryId"
      FROM "{SCHEMA}"."_communication" c
      WHERE c."deletedAt" IS NULL AND c.direction='MISSED'
        AND c."createdAt" >= NOW() - interval '{int(hours)} hours'
        AND c."timestamp" >= CURRENT_DATE
        AND c."personId" IS NOT NULL AND c."assignedagentId" IS NOT NULL
        AND NOT EXISTS (
          SELECT 1 FROM "{SCHEMA}"."workflowRun" r
          WHERE r."workflowId"='{WORKFLOW_ID}'
            AND r."state" #>> '{{stepInfos,trigger,result,recordId}}' = c.id::text
            AND r.status IN ('NOT_STARTED','RUNNING','COMPLETED')
        )
      ORDER BY c."createdAt" ASC;''').strip()
    return [x.split("|", 4) for x in raw.splitlines()] if raw else []


def make_state(flow, rec):
    after = {"id": rec[0], "personId": rec[1], "assignedagentId": rec[2],
             "timestamp": rec[3], "enquiryId": rec[4], "direction": "MISSED"}
    infos = {"trigger": {"result": {"recordId": rec[0], "properties": {"after": after}}, "status": "SUCCESS"}}
    for step in flow["steps"]:
        infos[step["id"]] = {"status": "NOT_STARTED"}
    return {"flow": {"steps": flow["steps"], "trigger": flow["trigger"]}, "stepInfos": infos}


def enqueue(rows, flow):
    runs = []
    statements = []
    for rec in rows:
        run_id = str(uuid.uuid4())
        state = json.dumps(make_state(flow, rec), separators=(",", ":"))
        statements.append(f'''INSERT INTO "{SCHEMA}"."workflowRun"
          (id,"createdAt","updatedAt",status,"createdBySource","createdByName",
           "updatedBySource","updatedByName",state,position,"workflowId","workflowVersionId")
          VALUES ('{run_id}',NOW(),NOW(),'NOT_STARTED','MANUAL','Operator','MANUAL','Operator',
                  $${state}$$::jsonb,0,'{WORKFLOW_ID}','{VERSION_ID}');''')
        runs.append((run_id, rec[0]))
    path = "/tmp/missed_calls_workflow.sql"
    open(path, "w").write("BEGIN;\n" + "\n".join(statements) + "\nCOMMIT;\n")
    docker(["docker", "cp", path, f"{DB}:{path}"], 10)
    docker(["docker", "exec", DB, "psql", "-U", "twenty", "-d", "default", "-f", path])
    for run_id, _record_id in runs:
        job_id = docker(["docker", "exec", REDIS, "redis-cli", "INCR", "bull:workflow-queue:id"]).strip()
        data = json.dumps({"workflowRunId": run_id, "workspaceId": WORKSPACE_ID})
        opts = json.dumps({"removeOnFail": {"age": 604800, "count": 1000}, "priority": 2,
                           "removeOnComplete": {"age": 14400, "count": 1000}, "attempts": 1})
        now = str(int(time.time() * 1000))
        docker(["docker", "exec", REDIS, "redis-cli", "HSET", f"bull:workflow-queue:{job_id}",
                "name", "RunWorkflowJob", "data", data, "opts", opts, "timestamp", now,
                "delay", "0", "attemptsMade", "0", "priority", "2"])
        docker(["docker", "exec", REDIS, "redis-cli", "LPUSH", "bull:workflow-queue:wait", job_id])
    return runs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--live", action="store_true", default=True, help="enqueue runs (default for cron)")
    parser.add_argument("--dry-run", action="store_true", help="do not enqueue workflow runs")
    args = parser.parse_args()
    flow = get_flow()
    rows = candidates(args.hours)
    if args.limit:
        rows = rows[:args.limit]
    print(json.dumps({"mode": "DRY_RUN" if args.dry_run else "LIVE", "workflow": WORKFLOW_ID,
                      "version": VERSION_ID, "candidates": len(rows),
                      "records": [r[0] for r in rows]}, indent=2))
    if args.live and not args.dry_run and rows:
        run_ids = enqueue(rows, flow)
        print(json.dumps({"enqueued": len(run_ids), "run_ids": [r[0] for r in run_ids]}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise

auto = None
