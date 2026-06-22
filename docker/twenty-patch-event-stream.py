#!/usr/bin/env python3
"""
Patch event-stream.service.js to handle Redis lock failures gracefully.

The bug: createEventStream/destroyEventStream/removeFromActiveStreams use
withLock() on the activeStreams key, but that key is also used by setAdd()
(as a Redis SET). The lock uses SET NX which fails because the key already
exists as a SET. This throws an unhandled error that crashes the process.

Fix: Wrap each withLock call in try/catch. On failure, log a warning and
proceed without the lock. Safe for single-server deployments.
"""
import re
import sys

FILE = "/app/packages/twenty-server/dist/engine/subscriptions/event-stream.service.js"

def patch_file(path):
    with open(path, "r") as f:
        content = f.read()

    if "proceeding without lock" in content:
        print("Already patched")
        return False

    # Pattern 1: createEventStream and destroyEventStream withLock blocks
    # These have the pattern:
    #   await this.cacheLockService.withLock(async ()=>{
    #       await this.cacheStorageService.setAdd/setRemove(activeStreamsKey, [...], ...);
    #   }, activeStreamsKey);
    #
    # We need to wrap them in try/catch and proceed without lock on failure.

    # Match: await this.cacheLockService.withLock(async ()=>{\n<indent>await this.cacheStorageService.<method>(activeStreamsKey, ...);\n<indent>}, activeStreamsKey);
    pattern = r'(        )await this\.cacheLockService\.withLock\(async \(\)=>\{(\n\s+await this\.cacheStorageService\.(setAdd|setRemove)\(activeStreamsKey, .+?\);)\n\s+\}, activeStreamsKey\);'

    def replacement(m):
        indent = m.group(1)
        inner = m.group(2)
        method = m.group(3)
        return f"""{indent}try {{
{indent}    await this.cacheLockService.withLock(async ()=>{{
{inner}
{indent}    }}, activeStreamsKey);
{indent}}} catch (lockError) {{
{indent}    this.logger.warn(`Failed to acquire lock for activeStreams, proceeding without lock: ${{lockError?.message ?? lockError}}`);
{indent}    {inner.strip()}
{indent}}}"""

    new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)

    if new_content == content:
        print("WARNING: Pattern not matched, trying alternate approach...")
        # Fallback: simpler line-by-line approach
        return patch_file_fallback(path)

    with open(path, "w") as f:
        f.write(new_content)

    print("Patched successfully")
    return True

def patch_file_fallback(path):
    """Fallback: line-by-line replacement"""
    with open(path, "r") as f:
        lines = f.readlines()

    new_lines = []
    i = 0
    patched = 0

    while i < len(lines):
        line = lines[i]

        # Look for: await this.cacheLockService.withLock(async ()=>{
        if 'await this.cacheLockService.withLock(async ()=>{' in line and i + 2 < len(lines):
            # Check if next lines contain setAdd/setRemove on activeStreamsKey
            next_lines = ''.join(lines[i:min(i+5, len(lines))])
            if 'activeStreamsKey' in next_lines and ('setAdd' in next_lines or 'setRemove' in next_lines):
                indent = '        '
                # Extract the inner block
                inner_lines = []
                j = i + 1
                while j < len(lines) and '}, activeStreamsKey);' not in lines[j]:
                    inner_lines.append(lines[j])
                    j += 1
                # Skip the closing line
                if j < len(lines):
                    j += 1

                # Write patched version
                new_lines.append(f'{indent}try {{\n')
                new_lines.append(f'{indent}    await this.cacheLockService.withLock(async ()=>{{\n')
                new_lines.extend(inner_lines)
                new_lines.append(f'{indent}    }}, activeStreamsKey);\n')
                new_lines.append(f'{indent}}} catch (lockError) {{\n')
                new_lines.append(f"{indent}    this.logger.warn(`Failed to acquire lock for activeStreams, proceeding without lock: ${{lockError?.message ?? lockError}}`);\n")
                for il in inner_lines:
                    stripped = il.strip()
                    if stripped:
                        new_lines.append(f'{indent}    {stripped}\n')
                new_lines.append(f'{indent}}}\n')

                i = j
                patched += 1
                continue

        new_lines.append(line)
        i += 1

    if patched > 0:
        with open(path, "w") as f:
            f.writelines(new_lines)
        print(f"Patched {patched} locations (fallback)")
        return True
    else:
        print("ERROR: Could not find patterns to patch")
        return False

if __name__ == "__main__":
    patch_file(FILE)
