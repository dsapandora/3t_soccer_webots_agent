"""agent_bridge — async RocketRide client wrapped for a sync Webots loop.

The Webots controller runs a synchronous step() loop at ~16ms. The RocketRide
SDK is asyncio-based, and an LLM round-trip takes 1-3s. To avoid blocking
the simulation we run the SDK on a background event loop in a daemon thread
and expose a tiny non-blocking API to the controller:

    bridge = AgentBridge(pipe_path='strategy.pipe')
    bridge.start()                          # returns once pipeline is up

    # In the Webots step loop, when robot is idle and no chat is in flight:
    bridge.submit_snapshot('ball:yes ...')  # fire-and-forget
    # The agent runs, calls MCP tools (turn_left, walk_forward, kick, ...)
    # which are queued onto mcp_server.BUS and consumed by the FSM. The
    # bridge's text answer is not parsed — MCP commands carry the plan.

    bridge.stop()                            # at controller exit

Critical patterns enforced:
- `client.use(filepath=..., use_existing=True)` — never restart a running pipe
  (avoids 'Pipeline Already Running' errors per RocketRide common mistakes).
- One in-flight chat() at a time — newer snapshots while a chat is pending
  are dropped (the controller will resubmit on next step).
- The asyncio loop NEVER blocks the Webots thread; submit_snapshot is
  thread-safe and returns immediately.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from typing import Optional


logger = logging.getLogger("soccerv3.agent_bridge")

# Quiet third-party libs — we only care about what the LLM decides.
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("websockets.client").setLevel(logging.WARNING)
logging.getLogger("rocketride").setLevel(logging.WARNING)
# MCP server + uvicorn: silence per-request access logs (each tool call
# normally prints "Processing CallToolRequest" + a 200 OK access line).
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)
logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)
logging.getLogger("mcp.server.streamable_http_manager").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)


_PLAN_KEYS = ("action", "params", "duration_ms", "rationale")

# Skill-name → (FSM_action, default duration_ms). When the LLM emits the
# tool name in markdown/prose instead of clean JSON, we fall back to
# matching the name and using sensible defaults. Includes both atomic L1
# skills and the composite L2 skills (kept for backward-compatibility).
_SKILL_FALLBACK = {
    # Atomic L1 — Claude orchestrates each step
    "walk":           ("WALK_FORWARD",   2000),
    "turn_left":      ("TURN_LEFT",      1500),
    "turn_right":     ("TURN_RIGHT",     1500),
    "step_back":      ("STEP_BACK",      1500),
    "sidestep_left":  ("SIDESTEP_LEFT",  2000),
    "sidestep_right": ("SIDESTEP_RIGHT", 2000),
    "walk_arc_left":  ("WALK_ARC_LEFT",  2500),
    "walk_arc_right": ("WALK_ARC_RIGHT", 2500),
    "look":           ("LOOK",            900),
    "kick":           ("KICK",           2500),
    "stop":           ("STOP",            600),
    # Composite L2 (legacy — only fire if Claude calls them)
    "find_ball":      ("FIND_BALL",     12000),
    "chase_ball":     ("CHASE_BALL",     8000),
    "drive_to_goal":  ("DRIVE_TO_GOAL", 10000),
    "celebrate":      ("CELEBRATE",      2000),
}


def _extract_plan(text: Optional[str]) -> Optional[dict]:
    """Pull a {action,params,duration_ms,rationale} plan out of an LLM
    answer string.

    Three strategies, in order:
      1. Strict JSON parse of the whole text.
      2. First balanced { ... } block.
      3. Skill-name fallback: scan for `find_ball.execute` / `chase_ball.execute`
         / `drive_to_goal.execute` / `celebrate.execute` in any prose/markdown.
    """
    if not text:
        return None
    s = text.strip()

    # Strategies 1 + 2: JSON-based.
    candidates = [s]
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if "action" not in obj or not isinstance(obj["action"], str):
            continue
        obj.setdefault("params", {})
        obj.setdefault("duration_ms", 3000)
        obj.setdefault("rationale", "")
        try:
            obj["duration_ms"] = int(obj["duration_ms"])
        except (TypeError, ValueError):
            obj["duration_ms"] = 3000
        if not isinstance(obj["params"], dict):
            obj["params"] = {}
        return obj

    # Strategy 3: skill-name fallback for synthesis-style answers.
    # Find the LAST mention so the most recent decision wins.
    matches = []
    for name, (action, dur) in _SKILL_FALLBACK.items():
        for m in re.finditer(rf"\b{re.escape(name)}(?:\.execute)?\b", s, re.IGNORECASE):
            matches.append((m.start(), name, action, dur))
    if matches:
        _, name, action, dur = sorted(matches)[-1]
        # Pull a one-line rationale from the answer (best-effort).
        rationale = (s.split("\n")[0] or s)[:200]
        return {
            "action": action,
            "params": {},
            "duration_ms": dur,
            "rationale": f"[fallback from prose: {name}] {rationale}",
        }
    return None


class AgentBridge:
    """Thin async-to-sync bridge over the RocketRide client."""

    def __init__(
        self,
        pipe_path: str = "strategy.pipe",
        ready_timeout_s: float = 30.0,
    ) -> None:
        self._pipe_path = os.path.abspath(pipe_path)
        self._ready_timeout = ready_timeout_s

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self._token: Optional[str] = None
        self._public_token: Optional[str] = None
        self._webhook_url: Optional[str] = None

        self._inflight = False
        self._inflight_lock = threading.Lock()

        self._ready = threading.Event()
        self._setup_error: Optional[BaseException] = None
        self._stop = threading.Event()

    # ---------------- public API (sync, called from Webots thread) ----------------

    def start(self) -> bool:
        """Start the bg thread and wait until pipeline is live.

        Returns True on success, False on timeout/setup error. Callers can
        keep the controller running in degraded (non-LLM) mode if False.
        """
        if self._thread is not None:
            return self._ready.is_set()
        self._thread = threading.Thread(
            target=self._run_loop, name="rocketride-bridge", daemon=True
        )
        self._thread.start()
        ok = self._ready.wait(timeout=self._ready_timeout)
        if not ok:
            logger.warning("AgentBridge: pipeline did not come up within %.1fs",
                           self._ready_timeout)
        if self._setup_error is not None:
            logger.error("AgentBridge setup error: %r", self._setup_error)
            return False
        return ok

    def submit_snapshot(self, snapshot_text: str) -> bool:
        """Fire-and-forget. Returns False if not ready or a chat is in flight."""
        if not self._ready.is_set() or self._stop.is_set():
            return False
        with self._inflight_lock:
            if self._inflight:
                return False
            self._inflight = True
        assert self._loop is not None  # set by _run_loop before _ready
        asyncio.run_coroutine_threadsafe(
            self._chat_once(snapshot_text), self._loop
        )
        return True

    def is_chat_inflight(self) -> bool:
        with self._inflight_lock:
            return self._inflight

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)

    # ---------------- internals (run in bg thread) ----------------

    def _run_loop(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._setup())
            if self._setup_error is None:
                self._ready.set()
                self._loop.run_forever()
        except Exception as exc:  # pragma: no cover — last-resort logging
            self._setup_error = exc
            self._ready.set()  # unblock caller even on failure
        finally:
            try:
                if self._loop is not None and not self._loop.is_closed():
                    self._loop.close()
            except Exception:
                pass

    async def _setup(self) -> None:
        try:
            from rocketride import RocketRideClient
        except ImportError as exc:
            self._setup_error = exc
            return
        # We need aiohttp for HTTP webhook posts (the SDK uses WebSocket DAP
        # but the webhook source only accepts plain HTTP POST).
        try:
            import aiohttp  # noqa: F401
        except ImportError as exc:
            self._setup_error = RuntimeError(
                "aiohttp is required for webhook source. Install: pip install aiohttp"
            )
            return
        try:
            # Load .env explicitly from the controller folder. The SDK reads
            # .env from os.getcwd() — when Webots launches us, cwd may differ
            # from this folder, so ${ROCKETRIDE_*} substitution fails silently
            # and the server gets the literal placeholder string.
            env = dict(os.environ)
            env_path = os.path.join(os.path.dirname(self._pipe_path), ".env")
            if os.path.isfile(env_path):
                for line in open(env_path, "r", encoding="utf-8"):
                    s = line.strip()
                    if not s or s.startswith("#") or "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    v = v.strip()
                    if (v.startswith('"') and v.endswith('"')) or (
                        v.startswith("'") and v.endswith("'")
                    ):
                        v = v[1:-1]
                    env[k.strip()] = v
                logger.info("loaded .env from %s (%d ROCKETRIDE_* vars)",
                            env_path,
                            sum(1 for k in env if k.startswith("ROCKETRIDE_")))
            else:
                logger.warning(".env not found at %s — env substitution will not work",
                               env_path)

            uri = env.get("ROCKETRIDE_URI", "")
            auth = env.get("ROCKETRIDE_APIKEY", "")
            self._client = RocketRideClient(uri=uri, auth=auth, env=env)
            await self._client.connect()

            # Sanity check: read the .pipe and run substitution ourselves so
            # we can see what the LLM nodes actually receive.
            try:
                import json
                with open(self._pipe_path, "r", encoding="utf-8") as f:
                    raw = f.read()
                substituted = raw
                for k, v in env.items():
                    if k.startswith("ROCKETRIDE_"):
                        substituted = substituted.replace("${" + k + "}", str(v))
                pipe_data = json.loads(substituted)
                for c in pipe_data.get("components", []):
                    if c.get("provider") == "llm_anthropic":
                        cfg = c.get("config", {})
                        prof = cfg.get("profile", "?")
                        prof_cfg = cfg.get(prof, {}) if isinstance(cfg.get(prof), dict) else {}
                        ak = prof_cfg.get("apikey", "")
                        looks_substituted = ak.startswith("sk-ant") if ak else False
                        logger.info(
                            "  llm_anthropic %s: profile=%s apikey_len=%d sk_ant=%s preview=%r",
                            c.get("id"), prof, len(ak), looks_substituted, ak[:12] + "…" if ak else "",
                        )
            except Exception as exc:
                logger.warning("could not preview substituted pipeline: %r", exc)
            # Pre-substitute env vars and pass the already-resolved pipeline
            # config via pipeline= instead of filepath=. This guarantees the
            # server receives the real apikey (not the ${ROCKETRIDE_*} literal),
            # bypassing any SDK substitution quirks.
            import json as _json
            with open(self._pipe_path, "r", encoding="utf-8") as f:
                _raw = f.read()
            for k, v in env.items():
                if k.startswith("ROCKETRIDE_"):
                    _raw = _raw.replace("${" + k + "}", str(v))
            pipeline_dict = _json.loads(_raw)
            logger.info("submitting pre-substituted pipeline (%d components, "
                        "project_id=%s)",
                        len(pipeline_dict.get("components", [])),
                        pipeline_dict.get("project_id"))
            result = await self._client.use(
                pipeline=pipeline_dict, use_existing=True
            )
            logger.info("client.use() result keys: %s", list(result.keys()) if isinstance(result, dict) else type(result).__name__)
            logger.info("client.use() full result: %r", result)
            self._token = result.get("token") if isinstance(result, dict) else None
            self._public_token = result.get("publicToken") if isinstance(result, dict) else None
            if not self._token:
                raise RuntimeError(
                    f"client.use() did not return a token: {result!r}"
                )
            # Derive HTTP base URL from the WebSocket URI for the webhook POST.
            # ws://host:port/task/service → http://host:port
            ws_uri = uri or "ws://localhost:5565"
            base = ws_uri.replace("ws://", "http://", 1).replace("wss://", "https://", 1)
            base = base.split("/task/")[0].rstrip("/")
            # Use the /webhook POST endpoint (registered as alias of /task/data).
            # The server reads Content-Type, opens a pipe with that mime_type,
            # streams the body, and returns the pipe close result. By sending
            # plain text the server constructs a Question internally.
            self._webhook_url = f"{base}/webhook?auth={self._public_token}"
            logger.info("AgentBridge ready, token=%s, endpoint=%s",
                        self._token[:8] + "…",
                        self._webhook_url.replace(self._public_token or "", "pk_…"))
        except Exception as exc:
            self._setup_error = exc

    async def _chat_once(self, snapshot_text: str) -> None:
        """Send the snapshot to the pipeline using client.send().

        client.send() does pipe.open/write/close WITHOUT specifying provider,
        so data flows to the pipeline's actual source node (not to a handler
        alias). pipe.close() then returns the real pipeline result including
        the agent's answers in 'answers' key.

        This is the pattern from purchase-order-processor (a working RocketRide
        project). HTTP POST to /webhook returns immediate ack only; client.send()
        is the right API.
        """
        import time
        t0 = time.monotonic()
        logger.info("→ send: %s", snapshot_text)
        try:
            from rocketride.schema import Question
            q = Question()
            q.addQuestion(snapshot_text)
            payload = q.model_dump_json().encode("utf-8")
            response = await self._client.send(
                self._token,
                data=payload,
                mimetype="application/rocketride-question",
            )
            elapsed = time.monotonic() - t0
            keys = list(response.keys()) if isinstance(response, dict) else type(response).__name__
            # If the agent finishes in <1s, something is wrong (LLM round-trip
            # alone is 1-3s minimum). Dump the full response so we can see
            # whether the pipe is dead, the agent errored, or the SDK is
            # short-circuiting somehow.
            if elapsed < 1.0:
                logger.warning("← FAST FINISH (%.2fs) — full response: %r",
                               elapsed, response)
            ans_text = ""
            if isinstance(response, dict):
                answers = response.get("answers")
                if isinstance(answers, list) and answers:
                    first = answers[0]
                    if isinstance(first, str):
                        ans_text = first
                    elif isinstance(first, dict):
                        for k in ("text", "answer", "content"):
                            v = first.get(k)
                            if isinstance(v, str):
                                ans_text = v
                                break
                        if not ans_text:
                            ans_text = json.dumps(first, default=str)
            preview = ans_text[:400] + ("…" if len(ans_text) > 400 else "")
            logger.info("← agent finished (%.1fs) keys=%s answer=%r",
                        elapsed, keys, preview)
            # Tool-node skills (find_ball/chase_ball/drive_to_goal/celebrate)
            # return a JSON action plan that lands in the answer. Parse it
            # and post to the MCP BUS so the FSM consumes it just like an
            # MCP tool call — single command queue regardless of source.
            plan = _extract_plan(ans_text)
            if plan is not None:
                try:
                    from mcp_server import BUS as _BUS
                    _BUS.post(plan)
                    logger.info("  → BUS: action=%s dur=%dms — %s",
                                plan.get("action"), plan.get("duration_ms"),
                                plan.get("rationale", ""))
                except Exception as exc:
                    logger.warning("BUS post failed: %r", exc)
        except Exception as exc:
            logger.exception("send() failed: %r", exc)
        finally:
            with self._inflight_lock:
                self._inflight = False

    async def _shutdown(self) -> None:
        try:
            if self._client is not None:
                await self._client.disconnect()
        except Exception:
            pass
        finally:
            assert self._loop is not None
            self._loop.stop()
