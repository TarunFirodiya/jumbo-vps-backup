#!/bin/bash
# Entrypoint wrapper for Twenty CRM that patches the event-stream lock bug
# before starting the main process

set -e

PATCH_FILE="/app/packages/twenty-server/dist/engine/subscriptions/event-stream.service.js"

apply_patch() {
  if [ ! -f "$PATCH_FILE" ]; then
    echo "[entrypoint] event-stream.service.js not found, skipping patch"
    return
  fi

  if grep -q "proceeding without lock" "$PATCH_FILE"; then
    echo "[entrypoint] Already patched"
    return
  fi

  echo "[entrypoint] Applying event-stream lock patch..."

  node -e "
const fs = require('fs');
const FILE = '$PATCH_FILE';
let content = fs.readFileSync(FILE, 'utf8');

if (content.includes('proceeding without lock')) {
  console.log('Already patched');
  process.exit(0);
}

const pattern = /        await this\.cacheLockService\.withLock\(async \(\)=>\{(\n\s+await this\.cacheStorageService\.(setAdd|setRemove)\(activeStreamsKey, .+?\);)\n        \}, activeStreamsKey\);/g;

let count = 0;
let match;
while ((match = pattern.exec(content)) !== null) count++;

if (count === 0) {
  console.log('No withLock blocks found, skipping');
  process.exit(0);
}

content = content.replace(pattern, (m, inner, method) => {
  return '        try {\n            await this.cacheLockService.withLock(async ()=>{' + inner + '            }, activeStreamsKey);\n        } catch (lockError) {\n            this.logger.warn(\`Failed to acquire lock for activeStreams, proceeding without lock: \${lockError?.message ?? lockError}\`);\n' + inner.trim() + '\n        }';
});

fs.writeFileSync(FILE, content);
console.log('Patched ' + count + ' withLock blocks');
"
}

apply_patch

# Now run the original command
echo "[entrypoint] Starting Twenty CRM..."
exec "$@"
