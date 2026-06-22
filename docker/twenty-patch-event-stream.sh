#!/bin/bash
# Patch event-stream.service.js to handle Redis lock failures gracefully
# This fixes the crash loop caused by the activeStreams Redis lock bug
# The lock key collides with a Redis SET (from setAdd), causing SET NX to always fail

FILE="/app/packages/twenty-server/dist/engine/subscriptions/event-stream.service.js"

if [ ! -f "$FILE" ]; then
  echo "event-stream.service.js not found, skipping patch"
  exit 0
fi

# Check if already patched
if grep -q "proceeding without lock" "$FILE"; then
  echo "Already patched"
  exit 0
fi

echo "Patching event-stream.service.js..."

# Patch createEventStream: wrap withLock in try/catch
sed -i 's/        await this.cacheLockService.withLock(async ()=>{\n            await this.cacheStorageService.setAdd(activeStreamsKey, \[\n                eventStreamChannelId\n            \], _eventstreamttlconstant.EVENT_STREAM_TTL_MS);\n        }, activeStreamsKey);/        try {\n            await this.cacheLockService.withLock(async ()=>{\n                await this.cacheStorageService.setAdd(activeStreamsKey, [\n                    eventStreamChannelId\n                ], _eventstreamttlconstant.EVENT_STREAM_TTL_MS);\n            }, activeStreamsKey);\n        } catch (lockError) {\n            this.logger.warn(`Failed to acquire lock for activeStreams, proceeding without lock: ${lockError?.message ?? lockError}`);\n            await this.cacheStorageService.setAdd(activeStreamsKey, [\n                eventStreamChannelId\n            ], _eventstreamttlconstant.EVENT_STREAM_TTL_MS);\n        }/' "$FILE"

echo "Patch applied successfully"
