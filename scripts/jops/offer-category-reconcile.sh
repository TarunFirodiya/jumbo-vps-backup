#!/bin/bash
# Reconcile active offer categories from completed visits.
# Rule: matching non-deleted Buyer Profile x Property visit, status COMPLETED,
# and scheduledAt strictly before offer.createdAt => AFTER_VISIT; else BEFORE_VISIT.
set -u
SCHEMA="workspace_1l3urgumjmspnjxohclmfz6fx"
LOG_FILE="/opt/jops/offer-category-reconcile.log"
LOCK_FILE="/opt/jops/offer-category-reconcile.lock"
MODE="${1:---live}"

if [ -f "$LOCK_FILE" ]; then
  pid=$(cat "$LOCK_FILE")
  if kill -0 "$pid" 2>/dev/null; then
    echo "$(date -Iseconds) SKIP: another instance running (PID $pid)"
    exit 0
  fi
  rm -f "$LOCK_FILE"
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

SQL=$(mktemp)
trap 'rm -f "$SQL" "$LOCK_FILE"' EXIT
cat > "$SQL" <<'EOSQL'
WITH computed AS (
  SELECT o.id,
    CASE WHEN EXISTS (
      SELECT 1
      FROM "workspace_1l3urgumjmspnjxohclmfz6fx"."_buyer" b
      JOIN "workspace_1l3urgumjmspnjxohclmfz6fx"."_visit" v
        ON v."buyerProfileId" = b.id
      WHERE b."deletedAt" IS NULL
        AND v."deletedAt" IS NULL
        AND b."personId" = o."pointOfContactId"
        AND v."propertyId" = o."propertyNewId"
        AND v.status = 'COMPLETED'
        AND v."scheduledAt" < o."createdAt"
    ) THEN 'AFTER_VISIT' ELSE 'BEFORE_VISIT' END::"workspace_1l3urgumjmspnjxohclmfz6fx"."opportunity_offerCategory_enum" AS new_category,
    o."offerCategory" AS old_category
  FROM "workspace_1l3urgumjmspnjxohclmfz6fx"."opportunity" o
  WHERE o."deletedAt" IS NULL
), summary AS (
  SELECT COUNT(*) AS checked,
    COUNT(*) FILTER (WHERE new_category = 'AFTER_VISIT') AS after_visit,
    COUNT(*) FILTER (WHERE new_category = 'BEFORE_VISIT') AS before_visit,
    COUNT(*) FILTER (WHERE old_category IS DISTINCT FROM new_category) AS changed
  FROM computed
)
SELECT 'SUMMARY|' || checked || '|' || after_visit || '|' || before_visit || '|' || changed FROM summary;
EOSQL

SUMMARY=$(docker cp "$SQL" twenty-db-1:/tmp/offer-category-reconcile.sql >/dev/null 2>&1 && docker exec twenty-db-1 psql -U twenty -d default -t -A -f /tmp/offer-category-reconcile.sql 2>&1)
if [ $? -ne 0 ]; then
  echo "$(date -Iseconds) ERROR: $SUMMARY" | tee -a "$LOG_FILE"
  exit 1
fi

if [ "$MODE" = "--dry-run" ]; then
  MSG="$(date -Iseconds) DRY_RUN $SUMMARY"
  echo "$MSG" | tee -a "$LOG_FILE"
  exit 0
fi

UPDATE_SQL=$(mktemp)
cat > "$UPDATE_SQL" <<'EOSQL'
WITH computed AS (
  SELECT o.id,
    CASE WHEN EXISTS (
      SELECT 1
      FROM "workspace_1l3urgumjmspnjxohclmfz6fx"."_buyer" b
      JOIN "workspace_1l3urgumjmspnjxohclmfz6fx"."_visit" v ON v."buyerProfileId" = b.id
      WHERE b."deletedAt" IS NULL AND v."deletedAt" IS NULL
        AND b."personId" = o."pointOfContactId"
        AND v."propertyId" = o."propertyNewId"
        AND v.status = 'COMPLETED' AND v."scheduledAt" < o."createdAt"
    ) THEN 'AFTER_VISIT' ELSE 'BEFORE_VISIT' END::"workspace_1l3urgumjmspnjxohclmfz6fx"."opportunity_offerCategory_enum" AS new_category
  FROM "workspace_1l3urgumjmspnjxohclmfz6fx"."opportunity" o
  WHERE o."deletedAt" IS NULL
)
UPDATE "workspace_1l3urgumjmspnjxohclmfz6fx"."opportunity" o
SET "offerCategory" = c.new_category, "updatedAt" = NOW()
FROM computed c
WHERE o.id = c.id AND o."offerCategory" IS DISTINCT FROM c.new_category;
EOSQL

docker cp "$UPDATE_SQL" twenty-db-1:/tmp/offer-category-reconcile-update.sql >/dev/null 2>&1
RESULT=$(docker exec twenty-db-1 psql -U twenty -d default -t -A -f /tmp/offer-category-reconcile-update.sql 2>&1)
if [ $? -ne 0 ]; then
  echo "$(date -Iseconds) ERROR_UPDATE: $RESULT" | tee -a "$LOG_FILE"
  exit 1
fi
UPDATED=$(printf '%s\n' "$RESULT" | grep -oE 'UPDATE [0-9]+' | awk '{print $2}' | tail -1)
MSG="$(date -Iseconds) OK $SUMMARY updated=${UPDATED:-0}"
echo "$MSG" | tee -a "$LOG_FILE"

auto_cleanup=1
rm -f "$SQL" "$UPDATE_SQL"
exit 0
chmod +x /opt/jops/offer-category-reconcile.sh
cp /opt/jops/offer-category-reconcile.sh /root/.hermes/profiles/operator/scripts/offer-category-reconcile.sh
chmod +x /root/.hermes/profiles/operator/scripts/offer-category-reconcile.sh
md5sum /opt/jops/offer-category-reconcile.sh /root/.hermes/profiles/operator/scripts/offer-category-reconcile.sh
/opt/jops/offer-category-reconcile.sh --dry-run
/root/.hermes/profiles/operator/scripts/offer-category-reconcile.sh --dry-run
