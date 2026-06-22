#!/bin/bash
# Dry run - just show what would be updated, don't actually update
DBPASS=$(grep PG_DATABASE_PASSWORD /opt/twenty/.env | head -1 | cut -d= -f2)

echo "=== DRY RUN: Buyer Stage Calculation ==="
echo ""

PGPASSWORD=*** docker exec -i twenty-db-1 psql -U twenty -d default -c "
SET search_path TO workspace_1l3urgumjmspnjxohclmfz6fx, public;

SELECT COUNT(*) as total_buyers FROM _buyer WHERE \"deletedAt\" IS NULL;
"

echo ""
echo "=== Current stage distribution ==="
PGPASSWORD=*** docker exec -i twenty-db-1 psql -U twenty -d default -c "
SET search_path TO workspace_1l3urgumjmspnjxohclmfz6fx, public;

SELECT \"leadStage\", COUNT(*) as cnt
FROM _buyer WHERE \"deletedAt\" IS NULL
GROUP BY \"leadStage\" ORDER BY cnt DESC;
"

echo ""
echo "=== Sample of what would change (first 10) ==="
PGPASSWORD=*** docker exec -i twenty-db-1 psql -U twenty -d default -c "
SET search_path TO workspace_1l3urgumjmspnjxohclmfz6fx, public;

WITH buyer_data AS (
    SELECT
        b.id,
        b.\"leadStage\" AS old_stage,
        b.\"createdAt\" AS buyer_created_at,
        (SELECT MAX(e.\"createdAt\") FROM _enquiry e WHERE e.\"buyerId\" = b.id AND e.\"deletedAt\" IS NULL) AS latest_enquiry_at,
        (SELECT MAX(v.\"createdAt\") FROM _visit v WHERE v.\"buyerProfileId\" = b.id AND v.\"deletedAt\" IS NULL) AS latest_visit_at
    FROM _buyer b
    WHERE b.\"deletedAt\" IS NULL
),
computed AS (
    SELECT
        id,
        old_stage,
        CASE
            WHEN latest_visit_at IS NOT NULL THEN
                CASE
                    WHEN EXTRACT(EPOCH FROM (NOW() - latest_visit_at)) / 86400 <= 30 THEN 'ACTIVE_VISITOR'
                    WHEN EXTRACT(EPOCH FROM (NOW() - latest_visit_at)) / 86400 <= 90 THEN 'AT_RISK_VISITOR'
                    ELSE 'INACTIVE'
                END
            WHEN latest_enquiry_at IS NOT NULL THEN
                CASE
                    WHEN EXTRACT(EPOCH FROM (NOW() - latest_enquiry_at)) / 86400 <= 7 THEN 'FRESH_LEAD'
                    WHEN EXTRACT(EPOCH FROM (NOW() - buyer_created_at)) / 86400 BETWEEN 8 AND 30 THEN 'AT_RISK_LEAD'
                    ELSE 'INACTIVE'
                END
            ELSE 'INACTIVE'
        END AS new_stage
    FROM buyer_data
)
SELECT old_stage, new_stage, COUNT(*) as cnt
FROM computed
WHERE old_stage IS NULL OR old_stage != new_stage
GROUP BY old_stage, new_stage
ORDER BY cnt DESC
LIMIT 20;
"

echo ""
echo "=== Count of rows that would be updated ==="
PGPASSWORD=*** docker exec -i twenty-db-1 psql -U twenty -d default -c "
SET search_path TO workspace_1l3urgumjmspnjxohclmfz6fx, public;

WITH buyer_data AS (
    SELECT
        b.id,
        b.\"leadStage\" AS old_stage,
        b.\"createdAt\" AS buyer_created_at,
        (SELECT MAX(e.\"createdAt\") FROM _enquiry e WHERE e.\"buyerId\" = b.id AND e.\"deletedAt\" IS NULL) AS latest_enquiry_at,
        (SELECT MAX(v.\"createdAt\") FROM _visit v WHERE v.\"buyerProfileId\" = b.id AND v.\"deletedAt\" IS NULL) AS latest_visit_at
    FROM _buyer b
    WHERE b.\"deletedAt\" IS NULL
),
computed AS (
    SELECT
        id,
        old_stage,
        CASE
            WHEN latest_visit_at IS NOT NULL THEN
                CASE
                    WHEN EXTRACT(EPOCH FROM (NOW() - latest_visit_at)) / 86400 <= 30 THEN 'ACTIVE_VISITOR'
                    WHEN EXTRACT(EPOCH FROM (NOW() - latest_visit_at)) / 86400 <= 90 THEN 'AT_RISK_VISITOR'
                    ELSE 'INACTIVE'
                END
            WHEN latest_enquiry_at IS NOT NULL THEN
                CASE
                    WHEN EXTRACT(EPOCH FROM (NOW() - latest_enquiry_at)) / 86400 <= 7 THEN 'FRESH_LEAD'
                    WHEN EXTRACT(EPOCH FROM (NOW() - buyer_created_at)) / 86400 BETWEEN 8 AND 30 THEN 'AT_RISK_LEAD'
                    ELSE 'INACTIVE'
                END
            ELSE 'INACTIVE'
        END AS new_stage
    FROM buyer_data
)
SELECT COUNT(*) as would_update FROM computed WHERE old_stage IS NULL OR old_stage != new_stage;
"
