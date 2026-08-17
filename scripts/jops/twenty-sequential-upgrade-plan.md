# Twenty CRM Sequential Upgrade Plan

Date prepared: 2026-08-16 IST
Production: Jumbo Homes / admin.jumbohomes.in

## Current state

- Runtime app env: `APP_VERSION=v2.14.4`
- Docker image: `twentycrm/twenty:latest`
- Current image digest: `sha256:fc6106b72c6a4fbabc763a68babd8609eeb385d660c61cd8c7000347330e34d6`
- Bundled client SDK reports `2.15.0`; this conflicts with APP_VERSION and must be resolved before upgrade.
- DB migration history contains completed migrations through 2.14 and earlier; no incomplete migration rows were found.
- Latest Docker Hub image checked: `v2.31.1`.
- Do not use floating `latest` for the upgrade. Pin every hop to an immutable version/digest.

## Version path — no intentional skips

Execute one release line at a time, with a health and regression gate after every hop:

1. v2.14.4 -> v2.15.0
2. v2.15.0 -> v2.16.0
3. v2.16.0 -> v2.17.0
4. v2.17.0 -> v2.18.0
5. v2.18.0 -> v2.19.0
6. v2.19.0 -> v2.20.0
7. v2.20.0 -> v2.21.0
8. v2.21.0 -> v2.22.0
9. v2.22.0 -> v2.23.2
10. v2.23.2 -> v2.24.0
11. v2.24.0 -> v2.25.0
12. v2.25.0 -> v2.26.0
13. v2.26.0 -> v2.27.0
14. v2.27.0 -> v2.28.0
15. v2.28.0 -> v2.29.0
16. v2.29.0 -> v2.30.0
17. v2.30.0 -> v2.30.1
18. v2.30.1 -> v2.31.1

There is no separate GitHub `twenty/v2.23.0` release and Docker Hub provides `v2.23.2`; use v2.23.2 as the published 2.23 patch line, and document that exception. This is not a jump over the 2.23 release line.

## Phase 0 — approval and freeze gate

- Upgrade is a production change and requires Tarun's explicit approval for the exact window.
- Do not execute routine upgrade work during Friday-Sunday freeze. The current CRM is reachable; schedule Monday or an approved maintenance window.
- No schema edits, permission edits, index creation, timeout changes, or application patches as part of this upgrade unless separately approved.

## Phase 1 — full backup and rollback capture

Before the first hop:

1. Create timestamped directory under `/opt/backups/twenty-upgrade-<timestamp>/`.
2. Copy `/opt/twenty/docker-compose.yml` and `/opt/twenty/.env` into it with restrictive permissions.
3. Save the current image digest and container inspect output.
4. Run a full PostgreSQL custom-format dump from `twenty-db-1`.
5. Verify the dump is non-empty and contains the PostgreSQL dump header.
6. Record Docker volumes, especially `db-data` and `server-local-data`.
7. Record current health, migration state, object/field counts, roles, views, API keys by name only, connected accounts by handle only, and webhooks.
8. Keep the current image locally; do not prune images or volumes.

Backup is a hard gate. If dump verification fails, stop.

## Phase 2 — per-hop procedure

For each pinned tag:

1. Read that version's GitHub release notes and identify breaking changes/migrations.
2. Check the tag exists on Docker Hub and record its amd64 digest.
3. Set `TAG=vX.Y.Z` in the environment used by Compose; do not leave `TAG=latest`.
4. Pull only the server/worker image for that tag.
5. Record the pre-hop image ID and migration state.
6. Recreate server/worker without removing DB or Redis volumes. Do not use `docker compose down`.
7. Wait for server health before starting/checking worker.
8. Monitor server logs for migrations, errors, restarts, and successful application start.
9. Verify worker is running and healthy after server health is established.
10. Verify internal and public health endpoints.
11. Verify the actual runtime version from `APP_VERSION` and the image/client metadata.
12. Verify migration rows: no non-completed migration rows; record the highest applied migration version/name.
13. Run regression checks before proceeding to the next hop.
14. If any gate fails, stop at that version and roll back to the immediately previous image. Do not continue.

## Phase 3 — regression gate after every hop

Minimum checks after every hop:

- `twenty-server-1` healthy
- `twenty-worker-1` running
- `twenty-db-1` healthy
- `twenty-redis-1` healthy
- Internal `/healthz` returns 200
- Public CRM health/site returns 200
- No new fatal/OOM/crash-loop errors
- No failed or pending migrations
- Custom object and field counts unchanged
- Roles and permission counts unchanged
- Views load
- API keys remain present by name
- Google connected accounts remain present by handle
- Webhook inventory unchanged
- One read-only API query each for building, property, buyer, enquiry, visit, seller and opportunity
- One controlled high-cardinality building/property read
- Check Caddy for new 502s during the test window

Do not proceed if a high-cardinality page regresses, even if `/healthz` is green.

## Important version-specific risks

### v2.14/v2.15 boundary

Known prior Jumbo upgrade risk: skipped workspace migrations can leave these columns missing or hidden:

- `core.view.isActive`
- `core.view.overrides`
- `core.objectMetadata.isUIEditable`
- `core.objectMetadata.isUICreatable`
- `core.fieldMetadata.isUIEditable`
- `core.commandMenuItem.isActive`
- `core.commandMenuItem.overrides`
- `core.connectedAccount.archivedAt`

Current read-only assessment confirms all eight physical columns exist. Still verify migration metadata/cache behavior after the hop.

### v2.17

Release notes include a change that caps to-many relation records per parent and inline chips. This is directly relevant to the current relation fan-out incident and must be tested against the failing building/property records.

### v2.19

Page layout type becomes required; inspect all existing page layouts before and after this hop.

### v2.20-v2.22

Workflow storage/core mirror migrations are introduced. Verify workflow definitions, active versions, runs and triggers after each hop. Do not assume the native CRON scheduler is healthy; compare against the existing known Hermes bypasses.

### v2.24

Upgrade migration fixes include a known self-hosted upgrade failure from 2.21.x involving a missing column. Treat migration logs as a hard gate.

### v2.25

Relevant performance changes:

- cap nested relation query concurrency
- field metadata/result processing caches
- API result handler deduplication
- respects `PG_POOL_MAX_CONNECTIONS`
- adds missing page-layout foreign-key indexes

This is a key performance validation point, not a reason to skip intermediate versions.

### v2.26

Breaking changes affect system View/viewField side effects and workflow core/flow storage flags. Verify layouts and workflows carefully.

### v2.28-v2.31

Verify row-level permission behavior on joined relations, nested relation widgets, workflow core consistency, metadata API compatibility, and any external consumers of metadata GraphQL. v2.30 includes a metadata GraphQL API breaking change for cursor types; inspect external integrations before crossing it.

## Final acceptance gate at v2.31.1

- Runtime version is confirmed as v2.31.1.
- All migrations completed.
- Full backup and rollback image remain available.
- Server/worker/DB/Redis healthy.
- Zero new heap-OOM events during a defined load test.
- Zero `QUERY_READ_TIMEOUT` events during high-cardinality record tests.
- No Caddy 502s attributable to Twenty during the test window.
- Building/property pages work, including the known high-cardinality records.
- Workflows, API integrations, Google accounts, storage, permissions and custom objects verified.
- Only then consider the upgrade complete.

## Rollback

At any failed gate:

- Stop advancing.
- Preserve logs and inspect output for the failed hop.
- Stop only server and worker.
- Restore the previous pinned image tag/digest.
- Start server, wait for health, then worker.
- Restore the database only if migrations changed the schema and application rollback is not compatible; never overwrite the DB casually.
- Verify all acceptance checks before reopening the CRM.

Never run `docker compose down`, `docker system prune`, or remove volumes as part of routine rollback.

## Current status

- Diagnostics complete and preserved at `/opt/jops/crm-502-evidence-20260816T164045Z`.
- Read-only pre-upgrade assessment at `/opt/jops/twenty-preupgrade-20260816T165253Z`.
- No production upgrade executed.
- Awaiting explicit approval for a weekday maintenance window and the sequential run.

## Official references

- https://docs.twenty.com/developers/self-hosting/introduction
- https://docs.twenty.com/developers/self-hosting/production
- https://github.com/twentyhq/twenty/releases
- https://hub.docker.com/r/twentycrm/twenty/tags
- Twenty upgrade skill: `devops/twenty-crm-docker`
- Pre-upgrade assessment: `references/pre-upgrade-impact-assessment.md`
- Breaking changes: `references/version-breaking-changes.md`
- v2.15 upgrade notes: `references/v215-upgrade-notes-jun2026.md`
- CRM safe operations: `devops/crm-safe-operations`
- VPS operations: `devops/vps-operations`

## Note

This is a plan artifact, not execution approval.
