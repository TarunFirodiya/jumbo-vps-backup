# Pending decision — zone-allocate-buildings fix

**Status:** AWAITING TARUN'S PICK. Do NOT apply until he answers AND it is Monday IST (weekend freeze).

**Failure:** `zone-allocate-buildings` cron failing since Aug 19 09:30 UTC (50/50 runs).
Root cause: JUM682 `_zoneallocation` row renamed Harlur → Haralur at Aug 19 09:05:35 UTC;
`/opt/jops/jum682-allocate-buildings.py` resolver only aliases Kadugodi→Kadugori.
Buildings in the Harlur polygon → `KeyError: 'Harlur'` before any write (no corruption).

**Option A (watchdog recommendation):** add `'Harlur': 'Haralur'` alias in the resolver,
mirroring the existing Kadugori alias. Then run `--check` mode and verify one clean
scheduled run before reporting done.

**Option B:** revert the CRM zone name back to `Harlur`.

**After either fix:** verify via `/opt/jops/zone-allocate-buildings.log` clean END line +
next scheduled cron run green, then patch `crm-routing-migrations` runbook with the
failure signature (zone rename breaks name-keyed resolvers) as prevention rule.

Found by proactive-system-ops-watchdog first live run, Sat 22 Aug 2026 07:03 IST.
