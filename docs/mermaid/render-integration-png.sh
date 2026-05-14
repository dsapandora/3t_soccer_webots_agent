#!/usr/bin/env bash
# Regenerate PNG figures from Mermaid sources (requires Node + npx + Chromium).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

BANNER="$ROOT/docs/3t-webots-readme-banner.png"
if [[ ! -f "$BANNER" ]]; then
  echo "Missing banner: $BANNER" >&2
  exit 1
fi

TMP_MMD="$ROOT/docs/mermaid/_webots-rocketride-integration.render.mmd"
export BANNER_PATH="$BANNER"
export ROOT_PATH="$ROOT"
export TMP_MMD_PATH="$TMP_MMD"
python3 <<'PY'
from pathlib import Path
import os
root = Path(os.environ["ROOT_PATH"])
banner = Path(os.environ["BANNER_PATH"]).resolve()
uri = banner.as_uri()
src = (root / "docs/mermaid/webots-rocketride-integration.mmd").read_text()
if "__BANNER_FILE_URL__" not in src:
    raise SystemExit("missing __BANNER_FILE_URL__ placeholder in webots-rocketride-integration.mmd")
out = src.replace("__BANNER_FILE_URL__", uri)
Path(os.environ["TMP_MMD_PATH"]).write_text(out)
PY

npx --yes @mermaid-js/mermaid-cli@11.4.0 \
  -i "$TMP_MMD" \
  -o docs/webots-rocketride-integration.png \
  -c docs/mermaid/integration-neon.json \
  -w 1800 -H 720 -b "#06060c"
rm -f "$TMP_MMD"
echo "Wrote docs/webots-rocketride-integration.png"
