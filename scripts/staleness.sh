#!/usr/bin/env bash
# staleness.sh — detect assurance registry staleness from git diff
#
# Compares the current worktree against the reviewed_commit in REGISTRY.yaml
# and reports which assurance components have stale watch_paths.
#
# Usage:
#   ./scripts/staleness.sh              # compare against reviewed_commit
#   ./scripts/staleness.sh <commit>     # compare against a specific commit
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REGISTRY="assurance/REGISTRY.yaml"

if [[ ! -f "$REGISTRY" ]]; then
    echo "ERROR: $REGISTRY not found" >&2
    exit 1
fi

find_python() {
    local candidate
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

find_python_with_module() {
    local module="$1"
    local candidate
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c "import $module" >/dev/null 2>&1; then
                printf '%s\n' "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON_ANY="$(find_python || true)"
PYTHON_YAML="$(find_python_with_module yaml || true)"

if [[ -z "$PYTHON_ANY" ]]; then
    echo "ERROR: Python not found" >&2
    exit 1
fi

# Extract reviewed_commit from REGISTRY.yaml
REVIEWED_COMMIT="${1:-}"
if [[ -z "$REVIEWED_COMMIT" ]]; then
    if [[ -n "$PYTHON_YAML" ]]; then
        REVIEWED_COMMIT=$($PYTHON_YAML -c "
import sys
import yaml
d = yaml.safe_load(open('$REGISTRY'))
print(d.get('reviewed_commit', ''))
")
    else
        REVIEWED_COMMIT=$($PYTHON_ANY -c "
import re
text = open('$REGISTRY').read()
m = re.search(r'^reviewed_commit:\s*(\S+)', text, re.MULTILINE)
print(m.group(1) if m else '')
")
    fi
fi

if [[ -z "$REVIEWED_COMMIT" ]]; then
    echo "ERROR: Could not extract reviewed_commit from $REGISTRY" >&2
    exit 1
fi

if ! git rev-parse --verify "${REVIEWED_COMMIT}^{commit}" >/dev/null 2>&1; then
    echo "ERROR: reviewed_commit '$REVIEWED_COMMIT' is not a valid commit" >&2
    exit 1
fi

if [[ -z "$PYTHON_YAML" ]]; then
    echo "ERROR: No Python with yaml support found for assurance component scan" >&2
    exit 1
fi

# Get list of changed files since reviewed_commit
CHANGED_FILES=$(git diff --name-only "$REVIEWED_COMMIT" HEAD)
UNCOMMITTED=$(git diff --name-only 2>/dev/null || true)
UNTRACKED=$(git ls-files --others --exclude-standard 2>/dev/null || true)
ALL_CHANGED=$(printf '%s\n%s\n%s' "$CHANGED_FILES" "$UNCOMMITTED" "$UNTRACKED" | sort -u | grep -v '^$' || true)

if [[ -z "$ALL_CHANGED" ]]; then
    echo "No files changed since reviewed_commit ($REVIEWED_COMMIT). All components fresh."
    exit 0
fi

# Check each component's watch_paths against changed files
$PYTHON_YAML - "$REVIEWED_COMMIT" <<'PY'
import fnmatch
import subprocess
import sys

import yaml

from pathlib import Path

reviewed_commit = sys.argv[1]
registry = yaml.safe_load(Path("assurance/REGISTRY.yaml").read_text())

result = subprocess.run(
    ["git", "diff", "--name-only", reviewed_commit, "HEAD"],
    capture_output=True,
    text=True,
)
if result.returncode != 0:
    print(result.stderr.strip() or "ERROR: git diff failed for reviewed_commit", file=sys.stderr)
    sys.exit(1)
changed = set(result.stdout.strip().splitlines()) if result.stdout.strip() else set()

# Also include uncommitted
result2 = subprocess.run(
    ["git", "diff", "--name-only"],
    capture_output=True,
    text=True,
)
if result2.returncode != 0:
    print(result2.stderr.strip() or "ERROR: git diff failed for worktree", file=sys.stderr)
    sys.exit(1)
if result2.stdout.strip():
    changed |= set(result2.stdout.strip().splitlines())

result3 = subprocess.run(
    ["git", "ls-files", "--others", "--exclude-standard"],
    capture_output=True,
    text=True,
)
if result3.returncode != 0:
    print(result3.stderr.strip() or "ERROR: git ls-files failed for untracked files", file=sys.stderr)
    sys.exit(1)
if result3.stdout.strip():
    changed |= set(result3.stdout.strip().splitlines())

# Strip rsi-econ/ prefix if present (git may include it from parent)
cleaned = set()
for f in changed:
    cleaned.add(f.removeprefix("rsi-econ/"))
changed = cleaned

stale_components = []

for component in registry.get("components", []):
    cid = component["id"]
    watch_paths = component.get("watch_paths", [])
    hits = []
    for pattern in watch_paths:
        for cf in changed:
            if fnmatch.fnmatch(cf, pattern):
                hits.append(cf)
    if hits:
        stale_components.append((cid, component.get("title", cid), hits))

print(f"Reviewed commit: {reviewed_commit}")
print(f"Changed files since then: {len(changed)}")
print()

if not stale_components:
    print("All assurance components are FRESH.")
else:
    print(f"{len(stale_components)} component(s) are STALE:\n")
    for cid, title, hits in stale_components:
        print(f"  {cid} ({title})")
        for h in sorted(set(hits))[:5]:
            print(f"    - {h}")
        if len(hits) > 5:
            print(f"    ... and {len(hits) - 5} more")
        print()

    print("Action: Review these components per assurance/RUNBOOK.md before")
    print("updating reviewed_commit in REGISTRY.yaml.")

sys.exit(1 if stale_components else 0)
PY
