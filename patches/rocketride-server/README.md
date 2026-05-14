# Patch: `rocketride-server` branch **AGENT-WEBOTS**

Non-upstream **Python tool nodes** and related **`agent_rocketride`** / image-upload helpers used by the Webots `strategy.pipe` demo. Upstream **[rocketride-server](https://github.com/rocketride-org/rocketride-server)** (`develop`) does not ship these; apply **`rocketride-server-AGENT-WEBOTS.patch`** after cloning.

## Shipped file

| File | Purpose |
|------|---------|
| **`rocketride-server-AGENT-WEBOTS.patch`** | Diff **`origin/develop…AGENT-WEBOTS`** limited to `nodes/src/nodes/…` (`tool_*`, `agent_rocketride`, `gpt_image_edit`, `openai_files_upload`) plus the small **`nodes/test/flow/`** updates from that branch. |

## Why we only ship the nodes patch

On the same **`AGENT-WEBOTS`** git branch there are also unrelated commits under **`docs/plans/`** and **`examples/`** (demos, not required for the Webots nodes). Some example pipes have contained **real API keys**, which [GitHub push protection](https://docs.github.com/code-security/secret-scanning/working-with-secret-scanning-and-push-protection/working-with-push-protection-from-the-command-line) rejects. Those paths are **not** part of this patch.

There are **no** branch changes outside `nodes/` that are required for registration: no root **`Cargo.toml`**, **`.rocketride/services-catalog.json`**, or engine **Rust** glue — each Python provider includes **`services.json`** in its own folder.

Verified with `git apply --check` against **`origin/develop`** at **`c89cdbcb`** (when the patch was generated).

## Apply

```bash
git clone https://github.com/rocketride-org/rocketride-server.git
cd rocketride-server
git checkout develop
git pull origin develop

PATCH=/path/to/3t_soccer_webots_agent/patches/rocketride-server/rocketride-server-AGENT-WEBOTS.patch
git apply --check "$PATCH"
git apply "$PATCH"
```

Then rebuild and run `rocketride-server` as usual.

## Regenerate

```bash
cd patches/rocketride-server
chmod +x generate-agent-webots-patch.sh
ROCKETRIDE_SERVER=/absolute/path/to/rocketride-server ./generate-agent-webots-patch.sh
```

| Variable | Default | Meaning |
|----------|---------|---------|
| `ROCKETRIDE_SERVER` | `../../../../rocketride-server` (from this folder) | Path to your `rocketride-server` git clone; override with an absolute path if yours lives elsewhere |
| `BASE_REF` | `origin/develop` | Diff base (`git fetch origin develop` first) |
| `FEATURE_REF` | `AGENT-WEBOTS` | Feature branch |
| `OUT` | `./rocketride-server-AGENT-WEBOTS.patch` | Output file |

**`apply-agent-webots-patch.sh`** — optional helper: `git apply --check` + `git apply`.

## Contents of the patch

- **`agent_rocketride`** — updates for multi-agent / MCP soccer flows.
- **`tool_*`** — stub tool providers aligned with soccer MCP names.
- **`gpt_image_edit`**, **`openai_files_upload`** — image payload support where needed.
- **`nodes/test/flow/…`** — flow test harness updates from the branch.
