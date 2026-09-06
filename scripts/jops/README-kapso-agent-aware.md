# Kapso Sync Agent-Aware Deployment
## t_06ff0652 — Ready for Sun 19:00+ IST

### Changes made (safe — code only, no DB)
1. Fixed `fmt_msgs_prosemirror()` — bare array format (was doc-wrapper, renders empty in CRM)
2. Fixed ProseMirror truncation `]` instead of `"}`
3. Fixed counter bug: `"skip"` normalized to `"skipped"` for accurate summary counts
4. Script at `/opt/jops/kapso-sync.py` — now agent-aware (was already v2.0 from Sep 4)

### Pending (weekend freeze — apply Sun 19:00+ IST)
5. Add `sellerId` + `propertyId` columns to `_communication` table
6. Register field metadata for CRM UI

### Deployment steps (Sun 19:00+ IST or whenever freeze lifts)
```bash
# Step 1: Apply DB schema changes
docker exec -i twenty-db-1 psql -U twenty -d default -f /opt/jops/deploy-seller-property-columns.sql

# Step 2: Verify columns exist
docker exec -i twenty-db-1 psql -U twenty -d default -t -A -c "
SELECT column_name, data_type FROM information_schema.columns
WHERE table_schema = 'workspace_1l3urgumjmspnjxohclmfz6fx'
  AND table_name = '_communication'
  AND column_name IN ('sellerId', 'propertyId');
"

# Step 3: Run a test sync (dry — just Ananya conversations first if possible)
python3 /opt/jops/kapso-sync.py

# Step 4: Flush Redis to clear any field metadata cache
docker exec twenty-redis-1 redis-cli FLUSHALL
```

### Rollback
```sql
ALTER TABLE workspace_1l3urgumjmspnjxohclmfz6fx._communication
  DROP COLUMN IF EXISTS "sellerId",
  DROP COLUMN IF EXISTS "propertyId";

DELETE FROM core."fieldMetadata"
WHERE "objectMetadataId" = 'fb6b73e1-a200-430f-95fb-28bdc20754cf'
  AND name IN ('seller', 'property');

DELETE FROM core."fieldMetadata"
WHERE "objectMetadataId" IN ('17cd93ba-6177-4f12-b1f8-3643eea152f7', '6dd67ff8-c3fc-4cf4-b7ec-6ca1a20b3deb')
  AND name = 'communication';
```

### Config reference
- Agent config: `/opt/jops/kapso_agent_config.json`
- Ananya → buyer pipeline → `enquiryId` linking
- Tara → seller pipeline → `sellerId` + `propertyId` linking