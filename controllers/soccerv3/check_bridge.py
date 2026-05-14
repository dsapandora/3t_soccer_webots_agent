"""Standalone bridge smoke test — no Webots needed.

Run from this folder:
    .venv/bin/python check_bridge.py

Starts the MCP server and the RocketRide bridge with .env values, sends one
synthetic world snapshot, and waits to see at least one MCP command land on
the BUS (e.g. turn_left/walk_forward/kick). Useful to confirm the pipeline
+ MCP wiring work before chasing controller bugs.
"""

from __future__ import annotations

import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Disable websocket payload truncation so we can see the full server response.
logging.getLogger("websockets.client").addFilter(
    lambda r: setattr(r, "msg", str(r.msg).replace("'", "'", 1)) or True
)
import websockets.protocol
try:
    websockets.protocol.MAX_LOG_BYTES = 64 * 1024
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from agent_bridge import AgentBridge  # noqa: E402
from mcp_server import BUS, start_mcp_server_in_thread  # noqa: E402

# Sanity-check env
for k in ("ROCKETRIDE_URI", "ROCKETRIDE_APIKEY", "ROCKETRIDE_ANTHROPIC_KEY"):
    v = os.environ.get(k) or "(reading from .env at runtime)"
    masked = v if len(v) < 12 else v[:8] + "…"
    print(f"  env  {k} = {masked}")

print("\nstarting MCP server (so the agent's mcp_client can connect)…")
start_mcp_server_in_thread()
# Publish a fake world state so mcp.get_world_state has something to read.
BUS.publish_world_state({
    "ball": {"visible": True, "x": -0.20, "y": 0.40, "area": 0.04},
    "goal": {"visible": True, "x": 0.10},
    "state": "IDLE",
})

bridge = AgentBridge(
    pipe_path=os.path.join(HERE, "strategy.pipe"),
    ready_timeout_s=60.0,
)
print("starting bridge…")
ok = bridge.start()
if not ok:
    print("[FAIL] bridge did not come up — see the error logged above.")
    sys.exit(1)

print("[OK] bridge up. Submitting one synthetic snapshot…")
snap = (
    "ball:yes ball_x:-0.20 ball_y:+0.40 ball_dist_est:0.55 "
    "goal:yes goal_x:+0.10 goal_y:-0.30 "
    "near_line:no line_side:none"
)
print(f"  snapshot: {snap}")
bridge.submit_snapshot(snap)

# Wait up to 30s for an MCP command to land on the BUS.
deadline = time.time() + 30.0
commands: list = []
while time.time() < deadline:
    commands = BUS.drain()
    if commands:
        break
    time.sleep(0.5)

if not commands:
    print("[FAIL] no MCP command received within 30s.")
    print("       check that strategy.pipe loads on the server, that")
    print("       mcp_client points at http://localhost:7777/mcp, and")
    print("       that the agent's tool calls reach the MCP server.")
    bridge.stop()
    sys.exit(1)

print(f"\n[OK] {len(commands)} MCP command(s) received:")
import json
for cmd in commands:
    print(json.dumps(cmd, indent=2))

bridge.stop()
print("\nAll good — pipeline + MCP wiring works end-to-end.")
