#!/usr/bin/env python3
"""
Hermes cron bypass for the Communications workflow.
Replaces the dead BullMQ WorkflowCronTriggerCronJob by:
  1. Inserting a workflowRun row into Twenty CRM
  2. Enqueuing a RunWorkflowJob on the BullMQ workflow-queue via Redis

Runs every 5 minutes via Hermes cron (no_agent mode).
Silent on success, prints to stdout on failure.
"""

import subprocess, json, uuid, time

WORKFLOW_ID     = "9272418a-906b-4091-9527-da0a053f2c5a"
VERSION_ID      = "3d9a2337-1aa1-4f24-a995-0b7c6cf1b3dc"
WORKSPACE_ID    = "1acb6d7e-22d6-44a0-95fa-fd1b7b7be25d"
SCHEMA          = "workspace_1l3urgumjmspnjxohclmfz6fx"

def run(cmd, check=True):
    """Run a command and return stdout, stderr, exit_code."""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nstderr: {r.stderr[:300]}")
    return r.stdout.strip(), r.stderr.strip(), r.returncode

def main():
    run_id = str(uuid.uuid4())

    # Load the workflow state template (flow + empty stepInfos)
    STATE_TEMPLATE = "/opt/jops/wf_communications_state.json"
    with open(STATE_TEMPLATE) as f:
        state_json = f.read().strip()

    # 1. Insert workflowRun with proper state
    # PostgreSQL dollar-quoting avoids escaping nightmares with nested JSON
    sql = f"""
    INSERT INTO "{SCHEMA}"."workflowRun"
      (id, "createdAt", "updatedAt", status,
       "createdBySource", "createdByName",
       "updatedBySource", "updatedByName",
       state, position, "workflowId", "workflowVersionId")
    VALUES
      ('{run_id}', NOW(), NOW(), 'NOT_STARTED',
       'MANUAL', 'Operator',
       'MANUAL', 'Operator',
       $STATE${state_json}$STATE$::jsonb, 0,
       '{WORKFLOW_ID}', '{VERSION_ID}');
    """
    # Write SQL to temp file to avoid quoting issues
    with open('/tmp/_wf_trigger.sql', 'w') as f:
        f.write(sql)
    subprocess.run(['docker', 'cp', '/tmp/_wf_trigger.sql', 'twenty-db-1:/tmp/_wf_trigger.sql'],
                   capture_output=True, timeout=5)
    run(['docker', 'exec', 'twenty-db-1', 'psql', '-U', 'twenty', '-d', 'default',
         '-f', '/tmp/_wf_trigger.sql'])

    # 2. Get next BullMQ job ID
    stdout, _, _ = run(['docker', 'exec', 'twenty-redis-1', 'redis-cli', 'INCR',
                        'bull:workflow-queue:id'])
    job_id = stdout.strip()

    # 3. Build job data
    job_data = json.dumps({
        "workflowRunId": run_id,
        "workspaceId": WORKSPACE_ID
    })
    job_opts = json.dumps({
        "removeOnFail": {"age": 604800, "count": 1000},
        "priority": 2,
        "removeOnComplete": {"age": 14400, "count": 1000},
        "attempts": 1
    })
    now_ms = str(int(time.time() * 1000))

    # 4. HSET the job hash
    run(['docker', 'exec', 'twenty-redis-1', 'redis-cli',
         'HSET', f'bull:workflow-queue:{job_id}',
         'name', 'RunWorkflowJob',
         'data', job_data,
         'opts', job_opts,
         'timestamp', now_ms,
         'delay', '0',
         'attemptsMade', '0',
         'priority', '2'])

    # 5. LPUSH to wait list
    run(['docker', 'exec', 'twenty-redis-1', 'redis-cli',
         'LPUSH', 'bull:workflow-queue:wait', job_id])

    # Silent on success — Hermes no_agent cron delivers stdout only if non-empty

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"FAILED: {e}", flush=True)
        raise SystemExit(1)