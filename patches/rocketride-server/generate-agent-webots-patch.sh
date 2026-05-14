#!/usr/bin/env bash
# Regenerate rocketride-server-AGENT-WEBOTS.patch (nodes + related flow tests only).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Monorepo: …/rocketride/projects/webots/patches/rocketride-server → ../../../../rocketride-server
ROCKETRIDE_SERVER="${ROCKETRIDE_SERVER:-$(cd "$SCRIPT_DIR/../../../../rocketride-server" 2>/dev/null && pwd || true)}"

BASE_REF="${BASE_REF:-origin/develop}"
FEATURE_REF="${FEATURE_REF:-AGENT-WEBOTS}"
OUT="${OUT:-$SCRIPT_DIR/rocketride-server-AGENT-WEBOTS.patch}"

if [[ ! -d "${ROCKETRIDE_SERVER}/.git" ]]; then
  echo "ERROR: ROCKETRIDE_SERVER must point to a git checkout of rocketride-server (got: ${ROCKETRIDE_SERVER:-empty})" >&2
  echo "  export ROCKETRIDE_SERVER=/absolute/path/to/rocketride-server" >&2
  exit 1
fi

cd "$ROCKETRIDE_SERVER"
git fetch origin develop 2>/dev/null || true

if ! git rev-parse --verify "$BASE_REF" >/dev/null 2>&1; then
  echo "ERROR: base ref '$BASE_REF' not found in $ROCKETRIDE_SERVER (try: git fetch origin)" >&2
  exit 1
fi
if ! git rev-parse --verify "$FEATURE_REF" >/dev/null 2>&1; then
  echo "ERROR: feature ref '$FEATURE_REF' not found." >&2
  exit 1
fi

echo "Writing patch → $OUT"
git diff "${BASE_REF}...${FEATURE_REF}" -- \
  nodes/src/nodes/agent_rocketride \
  nodes/src/nodes/gpt_image_edit \
  nodes/src/nodes/openai_files_upload \
  'nodes/src/nodes/tool_*' \
  nodes/test/flow/conftest.py \
  nodes/test/flow/test_llm_check_condition.py \
  nodes/test/flow/test_pyeval_check_condition.py \
  > "$OUT"
wc -l "$OUT" | awk '{print "Lines:", $1}'
