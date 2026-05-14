#!/usr/bin/env bash
# Apply rocketride-server-AGENT-WEBOTS.patch into a rocketride-server checkout.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH="${PATCH:-$SCRIPT_DIR/rocketride-server-AGENT-WEBOTS.patch}"
ROCKETRIDE_SERVER="${ROCKETRIDE_SERVER:-}"

if [[ -z "$ROCKETRIDE_SERVER" ]]; then
  echo "Usage: ROCKETRIDE_SERVER=/path/to/rocketride-server $0" >&2
  exit 1
fi
if [[ ! -f "$PATCH" ]]; then
  echo "ERROR: patch file not found: $PATCH" >&2
  echo "Run generate-agent-webots-patch.sh first." >&2
  exit 1
fi

cd "$ROCKETRIDE_SERVER"
git apply --check "$PATCH"
git apply "$PATCH"
echo "Applied $PATCH to $ROCKETRIDE_SERVER"
