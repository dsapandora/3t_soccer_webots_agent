"""soccerv3 — 3T-architecture Webots controller for the ROBOTIS-OP2 soccer demo.

Layer 1 (skills, fast loop):       soccerv3_native (C++ pybind11 binding over
                                   the ROBOTIS-OP2 Gait/Motion/Vision managers)
Layer 1.5 (perception, fast loop): vision.py (numpy HSV on the camera frame)
Layer 2 (sequencer):               fsm.py (state machine, reactive overrides)
Layer 3 (deliberative):            strategy.pipe + agent_bridge.py (Claude
                                   agent in RocketRide multi-agent pipeline)

The Webots step happens here every ~16ms. Each step we:
  1. Build a Snapshot from L1.5 (vision).
  2. Hand it to L2 (FSM) which either reacts immediately, applies the current
     L3 plan, or requests a new plan from the bridge (non-blocking).
  3. Advance the simulator by one step.

If the RocketRide bridge fails to come up (no .env, no server reachable, etc.)
the controller falls through to a degraded mode where the FSM only does
reactive behaviors (search/recover ball, avoid line, fall recovery). The
robot keeps moving — it just doesn't get LLM plans.
"""

from __future__ import annotations

import logging
import os
import sys

# Make the locally-built native extension and sibling modules importable
# when Webots launches us with cwd=this folder.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from agent_bridge import AgentBridge        # noqa: E402
from fsm import FSM                          # noqa: E402
from soccerv3_native import Soccerv3Native   # noqa: E402
from mcp_server import start_mcp_server_in_thread  # noqa: E402
import vision                                # noqa: E402


PIPE_FILENAME = "strategy.pipe"
# First-time startup with two llm_anthropic probes + multi-agent pipeline can
# take 30-60s easily (each Claude probe can be slow if Anthropic is loaded).
LLM_BRIDGE_TIMEOUT_S = 90.0
# MCP server port — local BUS + optional HTTP tools; FSM drains BUS after agent_bridge posts plans.
MCP_HOST = "127.0.0.1"
MCP_PORT = 7777


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run() -> None:
    _setup_logging()
    log = logging.getLogger("soccerv3")

    # --- L1: native robot ---
    robot = Soccerv3Native()
    log.info("Soccerv3Native instantiated, time_step=%dms", robot.time_step())

    # --- MCP server: expose robot tools to the RocketRide pipeline ---
    log.info("Starting MCP server on http://%s:%d/mcp …", MCP_HOST, MCP_PORT)
    start_mcp_server_in_thread(host=MCP_HOST, port=MCP_PORT)

    # --- L3: try to bring up the RocketRide bridge ---
    pipe_path = os.path.join(_HERE, PIPE_FILENAME)
    bridge: AgentBridge | None = None
    if os.path.isfile(pipe_path):
        bridge = AgentBridge(pipe_path=pipe_path, ready_timeout_s=LLM_BRIDGE_TIMEOUT_S)
        log.info("Starting RocketRide bridge with %s …", pipe_path)
        if not bridge.start():
            log.warning("RocketRide bridge unavailable — running in reactive-only mode.")
            bridge.stop(timeout_s=1.0)
            bridge = None
        else:
            log.info("RocketRide bridge ready.")
    else:
        log.warning("No %s next to controller — running in reactive-only mode.",
                    PIPE_FILENAME)

    # --- L2: FSM owns the loop logic ---
    fsm = FSM(robot, bridge)

    try:
        while True:
            snap = vision.snapshot(robot)
            fsm.step(snap)
            if not robot.step():
                break
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Shutting down …")
        if bridge is not None:
            bridge.stop()


if __name__ == "__main__":
    run()
