#!/bin/sh
# Entrypoint wrapper for Twenty CRM that patches the event-stream lock bug
# before starting the main process

set -e

PATCH_FILE="/app/packages/twenty-server/dist/engine/subscriptions/event-stream.service.js"

apply_patch() {
  if [ ! -f "$PATCH_FILE" ]; then
    echo "[entrypoint] event-stream.service.js not found, skipping patch"
    return
  fi

  # Check if already fully patched
  ALREADY=$(grep -c "proceeding without lock" "$PATCH_FILE" 2>/dev/null || echo 0)
  if [ "$ALREADY" -ge 3 ]; then
    echo "[entrypoint] All 3 withLock blocks already patched"
    return
  fi

  echo "[entrypoint] Applying event-stream lock patch..."

  node -e "
const fs = require('fs');
const FILE = '$PATCH_FILE';
let content = fs.readFileSync(FILE, 'utf8');

if (content.includes('proceeding without lock')) {
  console.log('Partially patched, re-applying...');
  // Remove existing partial patches first by restoring from backup pattern
}

// Match ALL withLock blocks with setAdd/setRemove on activeStreamsKey
const pattern = /([ \t]*)await this\.cacheLockService\.withLock\(async \(\)=>\{\n([ \t]*await this\.cacheStorageService\.(setAdd|setRemove)\(activeStreamsKey, [^)]+\);)\n[ \t]*\}, activeStreamsKey\);/g;

let count = 0;
let m;
while ((m = pattern.exec(content)) !== null) count++;

if (count === 0) {
  console.log('No withLock blocks found');
  process.exit(0);
}

content = content.replace(pattern, (match, indent, innerLine, method) => {
  const inner = innerLine.trim();
  return indent + 'try {\n' +
    indent + '    await this.cacheLockService.withLock(async ()=>{\n' +
    indent + inner + '\n' +
    indent + '    }, activeStreamsKey);\n' +
    indent + '} catch (lockError) {\n' +
    indent + '    this.logger.warn(\`Failed to acquire lock for activeStreams, proceeding without lock: \${lockError?.message ?? lockError}\`);\n' +
    indent + inner + '\n' +
    indent + '}';
});

fs.writeFileSync(FILE, content);
console.log('Patched ' + count + ' withLock block(s)');
"
}

apply_patch

# ── Patch 2: gaxios fetchImpl fix ──────────────────────────────
# gaxios v7.1.5 tries import('node-fetch').default on Node.js 24 CJS,
# which returns undefined instead of the fetch function.
# Fix: use globalThis.fetch before falling through to node-fetch.

apply_gaxios_patch() {
  GAXIOS_FILE="/app/node_modules/gaxios/build/cjs/src/gaxios.js"
  if [ ! -f "$GAXIOS_FILE" ]; then
    echo "[entrypoint] gaxios.js not found, skipping fetchImpl patch"
    return
  fi

  # Check if already patched
  if grep -q "globalThis.fetch" "$GAXIOS_FILE" 2>/dev/null; then
    echo "[entrypoint] gaxios fetchImpl already patched"
    return
  fi

  echo "[entrypoint] Applying gaxios fetchImpl patch..."

  node -e "
const fs = require('fs');
const file = '$GAXIOS_FILE';
let content = fs.readFileSync(file, 'utf8');
content = content.replace(
  /(this\\.#fetch \\|\\|= hasWindow\\n            \\? window\\.fetch\\n            : \\(await import\\('node-fetch'\\)\\)\\.default;)/,
  (match) => {
    return [
      'this.#fetch ||= hasWindow',
      '            ? window.fetch',
      '            : typeof globalThis.fetch === \"function\"',
      '                ? globalThis.fetch',
      '                : (await import(\"node-fetch\")).default;',
    ].join('\\n');
  }
);
fs.writeFileSync(file, content);
console.log('Patched gaxios fetchImpl');
"
}

apply_gaxios_patch

# Now run the original command
echo "[entrypoint] Starting Twenty CRM..."
exec "$@"
