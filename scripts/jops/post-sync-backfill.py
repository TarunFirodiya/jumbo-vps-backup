#!/usr/bin/env python3
"""
Post-sync backfill: fix any remaining mislabeled or unassigned records.
Run AFTER kapso-sync.py completes.

Usage: python3 post-sync-backfill.py
"""
import subprocess, sys

WORKSPACE = "workspace_1l3urgumjmspnjxohclmfz6fx"
AASHISH_ID = "404bdd9e-04c6-4ec6-a913-c9d98ab07c92"
SELLER_RELATIONS_ID = "217c3e8f-fe89-467c-aebd-0cbb0fe4f73e"

def run_sql(sql):
    cmd = ["docker", "exec", "-i", "twenty-db-1", "psql", "-U", "twenty",
           "-d", "default", "-t", "-A"]
    r = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=30)
    return r.stdout.strip()

print("=" * 60)
print("Post-sync backfill")

# T8: Rename convs where person has seller but name still says 'x Ananya'
print("\n[T8] Renaming 'x Ananya' -> 'x Tara' for seller-linked persons...")
t8_sql = f"""
UPDATE {WORKSPACE}._communication c
SET name = REPLACE(name, 'x Ananya', 'x Tara'),
    "updatedAt" = NOW()
WHERE c."communicationType" = 'WHATSAPP' AND c."deletedAt" IS NULL
  AND c.name LIKE '%x Ananya%'
  AND EXISTS (
    SELECT 1 FROM {WORKSPACE}._seller s
    WHERE s."personId" = c."personId" AND s."deletedAt" IS NULL
  );
"""
result = run_sql(t8_sql)
print(f"  [T8] Renamed records: {result if result else '0'}")

# T9: Fix NULL assignedagentId for WhatsApp convs
print("\n[T9] Setting assignedagentId for unassigned WhatsApp convs...")

# First: Ananya convs (no seller) -> Aashish
t9_ananya = f"""
UPDATE {WORKSPACE}._communication c
SET "assignedagentId" = '{AASHISH_ID}',
    "updatedAt" = NOW()
WHERE c."communicationType" = 'WHATSAPP' AND c."deletedAt" IS NULL
  AND c."assignedagentId" IS NULL
  AND c.name LIKE '%x Ananya%';
"""
r1 = run_sql(t9_ananya)
print(f"  [T9/Ananya] Set Aashish: {r1 if r1 else '0'}")

# Then: Tara convs (seller-linked) -> Seller Relations
t9_tara = f"""
UPDATE {WORKSPACE}._communication c
SET "assignedagentId" = '{SELLER_RELATIONS_ID}',
    "updatedAt" = NOW()
WHERE c."communicationType" = 'WHATSAPP' AND c."deletedAt" IS NULL
  AND c."assignedagentId" IS NULL
  AND c.name LIKE '%x Tara%';
"""
r2 = run_sql(t9_tara)
print(f"  [T9/Tara] Set Seller Relations: {r2 if r2 else '0'}")

# Remaining unassigned (neither pattern)
t9_other = f"""
SELECT COUNT(*) FROM {WORKSPACE}._communication
WHERE "communicationType" = 'WHATSAPP' AND "deletedAt" IS NULL
  AND "assignedagentId" IS NULL
  AND name NOT LIKE '%x Tara%' AND name NOT LIKE '%x Ananya%';
"""
r3 = run_sql(t9_other)
print(f"  [T9/Other] Still unassigned (needs review): {r3}")

print("\n" + "=" * 60)
print("Done.")