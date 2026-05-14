"""MCP server exposing primitive robot actions.

The agent commands every movement explicitly — there is no autonomous FSM
behavior beyond the boot greeting and fall recovery. Tools are primitive
(turn left, turn right, walk forward, stop, kick) so the LLM can compose a
search→find→approach→kick sequence step by step.

Architecture:
    Webots step loop (16ms)            MCP server thread
    ────────────────────────           ────────────────────
    fsm.step(snapshot)        ←────── command queue ←──── agent (tool call)
        applies pending command         (this module)
        publishes world state →────── world snapshot →──── get_world_state()
        publishes camera frame →─────── + JPEG image ────────────╮
                                                                 │
                                                  Claude SEES the frame
"""

from __future__ import annotations

import base64
import io
import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("soccerv3.mcp_server")


# ---------- Command bus ----------

@dataclass
class CommandBus:
    """Thread-safe bridge between MCP tool calls and the Webots FSM."""

    _commands: "queue.Queue[dict]" = field(default_factory=queue.Queue)
    _world_state_lock: threading.Lock = field(default_factory=threading.Lock)
    _world_state: dict = field(default_factory=dict)
    _image_lock: threading.Lock = field(default_factory=threading.Lock)
    # Latest camera frame as a base64-encoded JPEG. Encoded once per FSM
    # step so the MCP tool can return it without re-encoding under the lock.
    _image_b64: Optional[str] = None

    def post(self, cmd: dict) -> None:
        self._commands.put(cmd)

    def drain(self) -> list:
        out = []
        try:
            while True:
                out.append(self._commands.get_nowait())
        except queue.Empty:
            pass
        return out

    def publish_world_state(self, state: dict) -> None:
        with self._world_state_lock:
            self._world_state = dict(state)

    def get_world_state(self) -> dict:
        with self._world_state_lock:
            return dict(self._world_state)

    _pillow_warned: bool = False

    def publish_camera_image(self, rgb) -> None:
        """Encode an HxWx3 uint8 RGB array as JPEG and store base64."""
        try:
            from PIL import Image
            buf = io.BytesIO()
            Image.fromarray(rgb).save(buf, format="JPEG", quality=70)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except ImportError:
            if not self._pillow_warned:
                logger.warning(
                    "Pillow is NOT installed — Claude will not receive the "
                    "camera image. Install with: .venv/bin/pip install Pillow"
                )
                self._pillow_warned = True
            return
        except Exception as exc:
            logger.warning("camera image encode failed: %r", exc)
            return
        with self._image_lock:
            self._image_b64 = b64

    def get_camera_image_b64(self) -> Optional[str]:
        with self._image_lock:
            return self._image_b64


BUS = CommandBus()


# ---------- MCP server ----------

def build_mcp_server() -> Any:
    """Configure FastMCP with primitive robot-control tools."""
    import json
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ImageContent, TextContent

    mcp = FastMCP("soccerv3-robot", stateless_http=True)

    @mcp.tool()
    def get_world_state() -> list:
        """Read the latest perception snapshot AND the live camera image.

        Returns two MCP content blocks:
          1. TextContent — JSON dict with the numeric observations
             (ball, goal, line, state, goal_memory, current_time).
          2. ImageContent — the current camera frame as JPEG. Look at it
             yourself: identify ball / goal / lines / your facing direction.
             The numeric goal field can be unreliable; trust your eyes.

        Call this BEFORE every movement decision.
        """
        state = BUS.get_world_state()
        text = json.dumps(state, default=str)
        blocks: list = [TextContent(type="text", text=text)]
        img_b64 = BUS.get_camera_image_b64()
        if img_b64:
            blocks.append(ImageContent(
                type="image",
                data=img_b64,
                mimeType="image/jpeg",
            ))
        return blocks

    # Action skills are rocketride NODES in the pipeline graph (find_ball,
    # chase_ball, drive_to_goal, celebrate). MCP exposes only get_world_state
    # because it returns multimodal content (text + JPEG image).

    return mcp


def start_mcp_server_in_thread(host: str = "127.0.0.1", port: int = 7777) -> threading.Thread:
    """Launch the MCP server on a daemon thread (streamable-http at /mcp)."""

    def _run() -> None:
        try:
            mcp = build_mcp_server()
            mcp.settings.host = host
            mcp.settings.port = port
            logger.info("MCP server listening on http://%s:%d/mcp", host, port)
            mcp.run(transport="streamable-http")
        except Exception:
            logger.exception("MCP server crashed")

    t = threading.Thread(target=_run, name="soccerv3-mcp-server", daemon=True)
    t.start()
    return t
