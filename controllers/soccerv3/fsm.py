"""fsm — Layer 2 of the 3T architecture for soccerv3.

Translates the LLM agent's high-level plan into Layer-1 skill calls
(walk amplitudes, motion playbacks, kicks). Adds reactive overrides
that bypass the LLM entirely for safety-critical events (falls and
field-line proximity), so the robot never waits 1-3s on an LLM call
to do something obvious.

State diagram (informal):

    BOOT ──greeting──► IDLE ──snapshot──► [agent decides]
                               ◄──────────────────────────────
                                 │   │   │   │   │   │
                                 ▼   ▼   ▼   ▼   ▼   ▼
                       ATTACK_GOAL  DEFEND  RECOVER  AVOID  CELEBRATE  IDLE
                                 │
                       Reactive overrides (every step):
                         FALLEN_FORWARD/BACK ──recover motion──► IDLE
                         near_line=yes        ──RETREAT──► IDLE
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agent_bridge import AgentBridge
    from soccerv3_native import Soccerv3Native
    from vision import Snapshot


logger = logging.getLogger("soccerv3.fsm")


# ---------- Constants (kept in sync with soccerv2/Soccer.cpp) ----------

ACC_TOLERANCE = 80.0       # accelerometer dead-zone around 512 (1G)
ACC_FALL_STEPS = 20        # consecutive samples to confirm fall
KICK_TRIGGER_Y = 0.65      # ball normalized-y threshold to kick (high =
                           # ball near the bottom of the frame = at the feet).
                           # Soccer.cpp used 0.35 but its head-tracking gain
                           # is calibrated for that; with our +0.45 horizon
                           # bias we need a stricter threshold to avoid
                           # kicking the moment a 50cm-away ball is spotted.
HEAD_GAIN = 0.015          # rad/step head-tracking integration

# Motor indices that match Soccer.cpp's motorNames[] order.
MOTOR_NECK = 18
MOTOR_HEAD = 19

# Motion page IDs (RobotisOp2 default motion file).
MOTION_INIT = 1
MOTION_WALKREADY = 9
MOTION_RECOVER_FORWARD = 10
MOTION_RECOVER_BACK = 11
MOTION_KICK_RIGHT = 12
MOTION_KICK_LEFT = 13
MOTION_HELLO = 24


# ---------- LED palette (visual debug) ----------
LED_OK = 0x00FF00       # green
LED_BALL = 0x0000FF     # blue when tracking ball
LED_LOST = 0xFF0000     # red when searching
LED_THINK = 0xFFFF00    # yellow when waiting for LLM
LED_LINE = 0xFF8000     # orange when avoiding line
LED_CELEBRATE = 0xFF00FF  # magenta when celebrating


# ---------- State ----------

class State(Enum):
    BOOT = auto()           # greeting + walkready, then IDLE
    IDLE = auto()            # standing still, head still — waiting for LLM
    LOOK = auto()            # head/neck pose only, body still
    TURN_LEFT = auto()
    TURN_RIGHT = auto()
    WALK_FORWARD = auto()
    STEP_BACK = auto()       # small backstep to widen camera FOV
    SIDESTEP_LEFT = auto()   # lateral walk left, body facing forward
    SIDESTEP_RIGHT = auto()
    WALK_ARC_LEFT = auto()   # forward + curve left
    WALK_ARC_RIGHT = auto()
    CHASE_BALL = auto()      # reactive 60Hz chase+kick (soccerv2 logic)
    FIND_BALL = auto()       # head sweep + body rotation until ball found
    DRIVE_TO_GOAL = auto()   # chase the ball BIASED toward the goal, aim kick
    STOP = auto()            # explicit stop command from LLM
    KICK = auto()            # one-shot, returns to IDLE after motion
    CELEBRATE = auto()       # one-shot, returns to IDLE after motion
    FALLEN_FORWARD = auto()
    FALLEN_BACK = auto()


_ACTION_TO_STATE = {
    "LOOK": State.LOOK,
    "TURN_LEFT": State.TURN_LEFT,
    "TURN_RIGHT": State.TURN_RIGHT,
    "WALK": State.WALK_FORWARD,         # alias used by tool_walk minimal test
    "WALK_FORWARD": State.WALK_FORWARD,
    "STEP_BACK": State.STEP_BACK,
    "SIDESTEP_LEFT": State.SIDESTEP_LEFT,
    "SIDESTEP_RIGHT": State.SIDESTEP_RIGHT,
    "WALK_ARC_LEFT": State.WALK_ARC_LEFT,
    "WALK_ARC_RIGHT": State.WALK_ARC_RIGHT,
    "CHASE_BALL": State.CHASE_BALL,
    "FIND_BALL": State.FIND_BALL,
    "DRIVE_TO_GOAL": State.DRIVE_TO_GOAL,
    "STOP": State.STOP,
    "KICK": State.KICK,
    "CELEBRATE": State.CELEBRATE,
    "IDLE": State.IDLE,
}


@dataclass
class Plan:
    action: str
    params: dict
    duration_ms: int
    rationale: str
    deadline_t: float        # absolute robot time when this plan expires

    @classmethod
    def from_dict(cls, plan: dict, now: float) -> "Plan":
        ms = max(500, int(plan.get("duration_ms", 3000)))
        return cls(
            action=plan["action"],
            params=plan.get("params", {}) or {},
            duration_ms=ms,
            rationale=plan.get("rationale", ""),
            deadline_t=now + ms / 1000.0,
        )


# ---------- FSM ----------

class FSM:
    """L2 sequencer. Event-driven: each MCP command runs for its duration_ms,
    then the robot stops and a fresh snapshot is sent to wake the agent for
    the next command. The robot never moves on its own.
    """

    def __init__(
        self,
        robot: "Soccerv3Native",
        bridge: Optional["AgentBridge"],
    ) -> None:
        self.robot = robot
        self.bridge = bridge
        self.state: State = State.BOOT
        self.plan: Optional[Plan] = None

        # Fall counters (parity with Soccer.cpp).
        self._fup = 0
        self._fdown = 0

        # Head tracking integrator state.
        self._px = 0.0
        self._py = 0.0

        # BOOT step latch — we play the greeting once.
        self._booted = False

        # Spatial memory: remember when/where we last saw the goal so the
        # agent can reason about its direction even when it's currently out
        # of frame (e.g. after looking down at the ball).
        self._goal_last_seen_t: Optional[float] = None
        self._goal_last_seen_x: float = 0.0
        # Cumulative body yaw (radians) since last goal sighting. Updated
        # roughly per-step from a_amplitude + gait cadence so the agent has
        # a hint about the goal's current relative bearing.
        self._yaw_since_goal: float = 0.0

        # Persistent head/neck pose. set by _do_look and re-applied every
        # step (after gait_step) so the pose survives across plan expiries.
        # Without this, the gait would snap the head back to walkready and
        # the next snapshot would see the wrong frame. Default = horizon
        # (compensating for walkready torso lean — see _LOOK_PRESETS).
        self._head_target: float = self._HEAD_HORIZON_BIAS
        self._neck_target: float = 0.0

        # Pre-fetch state — set True after we ask for the next plan within
        # PLAN_PREFETCH_S of the current plan's deadline. Reset each time a
        # new plan arrives from the BUS. Avoids spamming snapshots.
        self._prefetched_for_plan: bool = False

    # Pre-fetch the next plan this many seconds before the current plan's
    # duration_ms expires. Tuned to roughly match Anthropic's ~13s round
    # trip — by the time the current plan ends, the next one is queued.
    PLAN_PREFETCH_S: float = 3.0

    # -------- main entry point, called from soccerv3.py every Webots step --------
    def step(self, snapshot: "Snapshot") -> None:
        # 0. Drain any pending MCP commands from the agent's tool calls. Each
        # command becomes a Plan that supersedes whatever we were doing.
        try:
            from mcp_server import BUS as _BUS
            for cmd in _BUS.drain():
                self.plan = Plan.from_dict(cmd, self.robot.current_time())
                self.state = _ACTION_TO_STATE.get(self.plan.action, State.IDLE)
                # New plan in flight — reset pre-fetch flag so the next
                # plan can be requested when this one is near its end.
                self._prefetched_for_plan = False
                # Reset the head integrator at the start of any chase/drive
                # so accumulated drift from previous tracking doesn't carry over.
                if self.state in (State.CHASE_BALL, State.DRIVE_TO_GOAL):
                    self._px = 0.0
                    self._py = 0.0
                logger.info("[mcp] action=%s params=%s dur=%dms — %s",
                            self.plan.action, self.plan.params,
                            self.plan.duration_ms, self.plan.rationale)

            now_t = self.robot.current_time()
            # Spatial memory: refresh whenever vision currently sees the goal.
            if snapshot.goal.visible:
                self._goal_last_seen_t = now_t
                self._goal_last_seen_x = snapshot.goal.x
                self._yaw_since_goal = 0.0

            # Build the agent-facing world snapshot. Includes goal_memory so
            # the agent can reason about the goal's direction even when it
            # isn't currently in frame.
            wm = {
                **snapshot.as_dict(),
                "state": self.state.name,
                "current_time": now_t,
                "goal_memory": self._goal_memory_dict(now_t),
            }
            _BUS.publish_world_state(wm)
            # Also publish the raw camera frame so get_world_state() can
            # return the JPEG to Claude.
            if snapshot._rgb is not None:
                _BUS.publish_camera_image(snapshot._rgb)
        except Exception:
            pass  # MCP server may not be running — that's OK

        # 1. Boot greeting (parity with soccerv2).
        if self.state is State.BOOT:
            self._do_boot()
            return

        now = self.robot.current_time()

        # 2. Reactive overrides (no LLM).
        # Falls always preempt — robot must recover before doing anything else.
        if self._update_fall_counters() and self._handle_fall():
            return

        # 3. Plan expiry: when the current MCP command's duration_ms is up,
        # stop moving and clear the plan. The robot now waits idle until the
        # agent issues the next MCP command. (debug-level — redundant with
        # the next "snapshot sent" log.)
        if self.plan is not None and now >= self.plan.deadline_t:
            logger.debug("[fsm] plan finished (%s)", self.plan.action)
            # If we just finished a chase/drive, the head was tracking the
            # ball at close range (pitched way down). Snap it back to the
            # horizon so the next snapshot sees the field, not the feet.
            if self.state in (State.CHASE_BALL, State.DRIVE_TO_GOAL):
                self._head_target = self._HEAD_HORIZON_BIAS
                self._neck_target = 0.0
                self._px = 0.0
                self._py = 0.0
            self.plan = None
            self.state = State.IDLE

        # 4. PRE-FETCH the next plan. The LLM takes ~13s to respond, so if
        # we wait until the current plan ends before asking, the robot
        # stops between actions. Instead, when there are PLAN_PREFETCH_S
        # seconds left on the current plan, submit a snapshot. The next
        # plan arrives via BUS.drain() on the next step and seamlessly
        # replaces the current one. No gap between actions.
        plan_should_prefetch = (
            self.plan is not None
            and not self._prefetched_for_plan
            and (self.plan.deadline_t - now) <= self.PLAN_PREFETCH_S
        )
        # Event-driven snapshot: send when we're idle (no plan) OR when we
        # should pre-fetch. The bridge's inflight guard prevents pile-ups.
        if self.bridge is not None and not self.bridge.is_chat_inflight():
            if self.plan is None or plan_should_prefetch:
                if self.bridge.submit_snapshot(snapshot.as_text()):
                    self.robot.set_eye_led(LED_THINK)
                    if plan_should_prefetch:
                        self._prefetched_for_plan = True
                        logger.info("[fsm] PRE-FETCH next plan (current %s ends in %.1fs)",
                                    self.plan.action,
                                    self.plan.deadline_t - now)
                    else:
                        logger.info("[fsm] snapshot sent — waiting for next command (%s)",
                                    snapshot.as_text())

        # 5. Apply current state. With no plan, this is STOP/IDLE (stand still).
        self._apply_state(snapshot)

    # -------- L2 → L1 dispatchers (primitive actions) --------
    def _apply_state(self, snapshot: "Snapshot") -> None:
        s = self.state
        if s is State.IDLE or s is State.STOP:
            self._do_stop()
        elif s is State.LOOK:
            direction = (self.plan.params.get("direction") if self.plan else None) or "center"
            self._do_look(direction)
        elif s is State.TURN_LEFT:
            self._do_turn(direction=+1.0)
        elif s is State.TURN_RIGHT:
            self._do_turn(direction=-1.0)
        elif s is State.WALK_FORWARD:
            self._do_walk_forward()
        elif s is State.STEP_BACK:
            self._do_step_back()
        elif s is State.SIDESTEP_LEFT:
            self._do_sidestep(direction=+1.0)
        elif s is State.SIDESTEP_RIGHT:
            self._do_sidestep(direction=-1.0)
        elif s is State.WALK_ARC_LEFT:
            self._do_walk_arc(direction=+1.0)
        elif s is State.WALK_ARC_RIGHT:
            self._do_walk_arc(direction=-1.0)
        elif s is State.CHASE_BALL:
            self._do_chase_ball(snapshot)
        elif s is State.FIND_BALL:
            self._do_find_ball(snapshot)
        elif s is State.DRIVE_TO_GOAL:
            self._do_drive_to_goal(snapshot)
        elif s is State.KICK:
            side = (self.plan.params.get("side") if self.plan else None) or "right"
            self._kick(side)
        elif s is State.CELEBRATE:
            self._do_celebrate()

        # Always re-assert the persistent head/neck pose AFTER any gait step.
        # Without this, a transient look(down) would relax back to walkready
        # as soon as the plan expires and gait_step takes over the body.
        self.robot.set_motor_position(MOTOR_NECK, self._neck_target)
        self.robot.set_motor_position(MOTOR_HEAD, self._head_target)

    # -------- Goal memory --------
    def _goal_memory_dict(self, now: float) -> dict:
        """Build the goal_memory block included in get_world_state.

        Fields:
          ever_seen: have we ever spotted the goal?
          seconds_ago: time since the most recent sighting (None if never).
          last_x: in-frame x (-1..+1) at the moment of that sighting.
          yaw_drift_rad: rough cumulative body yaw since the sighting
              (positive = body has rotated LEFT, negative = RIGHT). Use
              this to estimate the goal's current bearing relative to the
              robot's current facing.
        """
        if self._goal_last_seen_t is None:
            return {
                "ever_seen": False,
                "seconds_ago": None,
                "last_x": 0.0,
                "yaw_drift_rad": 0.0,
            }
        return {
            "ever_seen": True,
            "seconds_ago": round(now - self._goal_last_seen_t, 2),
            "last_x": round(self._goal_last_seen_x, 3),
            "yaw_drift_rad": round(self._yaw_since_goal, 3),
        }

    # -------- BOOT --------
    def _do_boot(self) -> None:
        if self._booted:
            self.state = State.IDLE
            return
        logger.info("[boot] greeting + walkready (parity with soccerv2)")
        self.robot.speak(
            "Hi, my name is ROBOTIS OP2. I can walk, use my camera to find "
            "the ball, and perform complex motion like kicking the ball "
            "for example.",
            1.0,
        )
        # Advance one step so sensors are valid.
        if not self.robot.step():
            return
        self.robot.set_eye_led(LED_OK)
        # Minimal boot — INIT + WALKREADY for a stable standing pose only.
        # No greeting/HELLO motion. The robot does NOTHING autonomous beyond
        # this and fall recovery; everything else waits for Claude's command.
        self.robot.play_motion(MOTION_INIT)
        self.robot.play_motion(MOTION_WALKREADY)
        self.robot.wait_ms(200)
        self.robot.gait_start()
        self.robot.gait_step()
        # Force head to horizon BEFORE the first snapshot — without this
        # the agent's first frame would show the floor (walkready leans
        # forward and the servo needs ~10 steps to reach +0.45 rad).
        for _ in range(30):
            self.robot.set_motor_position(MOTOR_NECK, 0.0)
            self.robot.set_motor_position(MOTOR_HEAD, self._HEAD_HORIZON_BIAS)
            if not self.robot.step():
                return
        logger.info("[boot] head locked to horizon (head=%.2f rad)",
                    self._HEAD_HORIZON_BIAS)
        self._booted = True
        self.state = State.IDLE

    # -------- Reactive: falls --------
    def _update_fall_counters(self) -> bool:
        acc = self.robot.accelerometer()
        if acc[1] < 512.0 - ACC_TOLERANCE:
            self._fup += 1
        else:
            self._fup = 0
        if acc[1] > 512.0 + ACC_TOLERANCE:
            self._fdown += 1
        else:
            self._fdown = 0
        return self._fup > ACC_FALL_STEPS or self._fdown > ACC_FALL_STEPS

    def _handle_fall(self) -> bool:
        if self._fup > ACC_FALL_STEPS:
            logger.info("[reactive] fall forward → recover")
            self.robot.play_motion(MOTION_INIT)
            self.robot.play_motion(MOTION_RECOVER_FORWARD)
            self.robot.play_motion(MOTION_WALKREADY)
            self._fup = 0
            self._head_target = self._HEAD_HORIZON_BIAS
            self._neck_target = 0.0
            self.state = State.IDLE
            self.plan = None
            return True
        if self._fdown > ACC_FALL_STEPS:
            logger.info("[reactive] fall back → recover")
            self.robot.play_motion(MOTION_INIT)
            self.robot.play_motion(MOTION_RECOVER_BACK)
            self.robot.play_motion(MOTION_WALKREADY)
            self._fdown = 0
            self._head_target = self._HEAD_HORIZON_BIAS
            self._neck_target = 0.0
            self.state = State.IDLE
            self.plan = None
            return True
        return False


    # -------- Primitive action implementations --------
    def _do_stop(self) -> None:
        """Stand still — no walking, no head movement. Pure waiting."""
        self.robot.set_eye_led(LED_OK)
        self.robot.set_x_amplitude(0.0)
        self.robot.set_y_amplitude(0.0)
        self.robot.set_a_amplitude(0.0)
        self.robot.gait_step()

    def _do_turn(self, direction: float) -> None:
        """Rotate in place. direction: +1.0 = left, -1.0 = right."""
        self.robot.set_eye_led(LED_LOST)
        self.robot.set_x_amplitude(0.0)
        self.robot.set_y_amplitude(0.0)
        a = 0.4 * direction
        self.robot.set_a_amplitude(a)
        self.robot.gait_step()
        # Track cumulative yaw so goal_memory can estimate current bearing.
        self._yaw_since_goal += a * (self.robot.time_step() / 1000.0)

    def _do_walk_forward(self) -> None:
        """Walk straight forward."""
        self.robot.set_eye_led(LED_BALL)
        self.robot.set_x_amplitude(0.8)
        self.robot.set_y_amplitude(0.0)
        self.robot.set_a_amplitude(0.0)
        self.robot.gait_step()

    def _do_step_back(self) -> None:
        """Walk a small step backward to widen the camera FOV (perception
        move, not retreat). Uses a smaller magnitude than forward so the
        backstep stays short.
        """
        self.robot.set_eye_led(LED_LOST)
        self.robot.set_x_amplitude(-0.5)
        self.robot.set_y_amplitude(0.0)
        self.robot.set_a_amplitude(0.0)
        self.robot.gait_step()

    def _do_sidestep(self, direction: float) -> None:
        """Lateral walk. direction: +1.0 = left, -1.0 = right.
        Body faces forward — used to circle the ball without losing sight.
        """
        self.robot.set_eye_led(LED_BALL)
        self.robot.set_x_amplitude(0.0)
        self.robot.set_y_amplitude(0.5 * direction)
        self.robot.set_a_amplitude(0.0)
        self.robot.gait_step()

    def _do_walk_arc(self, direction: float) -> None:
        """Walk forward while curving. direction: +1.0 = left, -1.0 = right."""
        self.robot.set_eye_led(LED_BALL)
        self.robot.set_x_amplitude(0.7)
        self.robot.set_y_amplitude(0.0)
        a = 0.25 * direction
        self.robot.set_a_amplitude(a)
        self.robot.gait_step()
        self._yaw_since_goal += a * (self.robot.time_step() / 1000.0)

    def _do_find_ball(self, snapshot: "Snapshot") -> None:
        """Autonomous ball search (soccerv2 port). Sweeps the head pitch
        in a sinusoid while slowly rotating the body. Ends as soon as the
        ball comes into view, so the agent's next snapshot will see it.
        """
        if snapshot.ball.visible:
            # Found it! Park head at the ball's last seen pitch and exit
            # FIND_BALL. The agent's next decision can now call chase_ball.
            self._head_target = self._HEAD_HORIZON_BIAS
            self._neck_target = 0.0
            self._px = 0.0
            self._py = 0.0
            self.plan = None
            self.state = State.IDLE
            logger.info("[fsm] find_ball: ball acquired (x=%.2f y=%.2f)",
                        snapshot.ball.x, snapshot.ball.y)
            return

        # Sinusoidal head pitch sweep — cover floor → horizon → up → and
        # back. Period ~3.1s (2*pi/2.0 from the sin argument).
        t = self.robot.current_time()
        head_pitch = 0.7 * math.sin(2.0 * t)
        self._head_target = _clamp(
            head_pitch,
            self.robot.min_motor_position(MOTOR_HEAD),
            self.robot.max_motor_position(MOTOR_HEAD),
        )
        self._neck_target = 0.0
        # Slowly rotate the body so the search covers a full circle in
        # roughly 20s; the gait yaw rate accumulates into goal_memory too.
        a = -0.3
        self.robot.set_eye_led(LED_LOST)
        self.robot.set_x_amplitude(0.0)
        self.robot.set_y_amplitude(0.0)
        self.robot.set_a_amplitude(a)
        self.robot.gait_step()
        self._yaw_since_goal += a * (self.robot.time_step() / 1000.0)

    def _do_chase_ball(self, snapshot: "Snapshot") -> None:
        """Closed-loop reactive chase + auto-kick (soccerv2 port).

        Runs every Webots step (~16ms) while CHASE_BALL is active. Mirrors
        the original Soccer.cpp main-loop: track ball with head, walk
        toward it, kick when close. Way faster than letting the LLM steer
        each primitive at 15s/decision.
        """
        if not snapshot.ball.visible:
            # Lost sight: stop walking, reset the head to horizon and end
            # the chase early. Without the head reset the camera would stay
            # pointed at the robot's own feet (last tracked pose) and the
            # next snapshot would be useless for re-acquiring the ball.
            self.robot.set_eye_led(LED_LOST)
            self.robot.set_x_amplitude(0.0)
            self.robot.set_y_amplitude(0.0)
            self.robot.set_a_amplitude(0.0)
            self.robot.gait_step()
            self._head_target = self._HEAD_HORIZON_BIAS
            self._neck_target = 0.0
            self._px = 0.0
            self._py = 0.0
            # End the chase plan so the agent replans immediately on the
            # next snapshot instead of waiting for the 8s deadline.
            self.plan = None
            self.state = State.IDLE
            return

        bx, by = snapshot.ball.x, snapshot.ball.y

        # Track ball with head/neck (Soccer.cpp's leaky-integrator style).
        self._px = HEAD_GAIN * bx + self._px
        self._py = HEAD_GAIN * by + self._py
        self._neck_target = _clamp(
            -self._px,
            self.robot.min_motor_position(MOTOR_NECK),
            self.robot.max_motor_position(MOTOR_NECK),
        )
        self._head_target = _clamp(
            -self._py,
            self.robot.min_motor_position(MOTOR_HEAD),
            self.robot.max_motor_position(MOTOR_HEAD),
        )

        # Walk toward ball: faster when ball is far (high in frame, by<0.1),
        # slower when close. Body yaw follows the neck so we curve toward
        # the ball.
        x_amp = 1.0 if by < 0.1 else 0.5
        self.robot.set_eye_led(LED_BALL)
        self.robot.set_x_amplitude(x_amp)
        self.robot.set_y_amplitude(0.0)
        self.robot.set_a_amplitude(self._neck_target)
        self.robot.gait_step()

        # Kick when ball reaches the feet (Soccer.cpp uses y>0.35).
        if by > KICK_TRIGGER_Y:
            side = "left" if bx < 0.0 else "right"
            self._kick(side)  # _kick clears plan and sets state=IDLE

    def _do_drive_to_goal(self, snapshot: "Snapshot") -> None:
        """Drive the ball to the goal: chase + body bias toward goal +
        only kick when the goal is roughly ahead so the shot has direction.

        Combines chase_ball reactivity with goal-awareness:
          - Tracks ball with head (same as chase_ball).
          - Body yaw biased TOWARD the goal (goal.x or goal_memory hint).
          - When ball is at feet, ONLY kicks if goal is roughly ahead
            (|goal_x| < 0.5). Otherwise keeps walking/turning to align.
        """
        if not snapshot.ball.visible:
            self.robot.set_eye_led(LED_LOST)
            self.robot.set_x_amplitude(0.0)
            self.robot.set_y_amplitude(0.0)
            self.robot.set_a_amplitude(0.0)
            self.robot.gait_step()
            self._head_target = self._HEAD_HORIZON_BIAS
            self._neck_target = 0.0
            self._px = 0.0
            self._py = 0.0
            self.plan = None
            self.state = State.IDLE
            return

        bx, by = snapshot.ball.x, snapshot.ball.y

        # Head tracks ball (same leaky integrator as chase_ball).
        self._px = HEAD_GAIN * bx + self._px
        self._py = HEAD_GAIN * by + self._py
        self._neck_target = _clamp(
            -self._px,
            self.robot.min_motor_position(MOTOR_NECK),
            self.robot.max_motor_position(MOTOR_NECK),
        )
        self._head_target = _clamp(
            -self._py,
            self.robot.min_motor_position(MOTOR_HEAD),
            self.robot.max_motor_position(MOTOR_HEAD),
        )

        # Estimate goal direction relative to current facing.
        goal_x: Optional[float] = None
        if snapshot.goal.visible:
            goal_x = snapshot.goal.x
        elif self._goal_last_seen_t is not None:
            # Memory-based estimate: last_x adjusted by yaw_drift since.
            goal_x = self._goal_last_seen_x - self._yaw_since_goal

        # Body yaw: follow head (toward ball) + a small bias toward the goal
        # so we approach the ball from a side that lets us kick toward goal.
        a_amp = self._neck_target
        if goal_x is not None:
            # Sign convention: positive a_amp = turn LEFT. To turn LEFT we
            # want to face something with negative x (in image coords). So
            # bias = -goal_x * gain.
            a_amp += 0.15 * (-goal_x)
            a_amp = max(-0.5, min(0.5, a_amp))

        x_amp = 1.0 if by < 0.1 else 0.5
        self.robot.set_eye_led(LED_BALL)
        self.robot.set_x_amplitude(x_amp)
        self.robot.set_y_amplitude(0.0)
        self.robot.set_a_amplitude(a_amp)
        self.robot.gait_step()

        # Kick only if ball is at feet AND goal is roughly straight ahead.
        if by > KICK_TRIGGER_Y:
            if goal_x is not None and abs(goal_x) < 0.5:
                # Aligned shot — pick foot toward goal so the kick sweeps
                # the ball roughly toward it.
                side = "left" if goal_x < 0.0 else "right"
                logger.info("[fsm] drive_to_goal: SHOT (goal_x=%.2f, side=%s)",
                            goal_x, side)
                self._kick(side)
            elif goal_x is None:
                # No goal info — fall back to chase-style kick (better than
                # not kicking at all when the ball is at our feet).
                side = "left" if bx < 0.0 else "right"
                logger.info("[fsm] drive_to_goal: blind kick (no goal info)")
                self._kick(side)
            # else: goal off to the side, keep walking/turning to align

    # Head/neck pose presets (radians). Values are clamped to motor limits
    # at runtime. Convention:
    #   NECK positive = head turned LEFT (yaw)
    #   HEAD positive = pitched UP (matches Soccer.cpp's headPosition=-y;
    #     ball above center y<0 → headPosition>0 → pitch up)
    # NB: motor "0" is head straight relative to TORSO, but the ROBOTIS-OP2
    # walkready stance leans the torso forward ~20-25°, so head=0 actually
    # points the camera DOWN at the floor (you see the robot's own feet).
    # To see the horizon we need a positive bias of ~+0.45 rad to compensate.
    _HEAD_HORIZON_BIAS = 0.45
    _LOOK_PRESETS = {
        "center": (0.0, _HEAD_HORIZON_BIAS),       # actual horizon
        "down":   (0.0, -0.5),                     # see feet
        "up":     (0.0, _HEAD_HORIZON_BIAS + 0.5), # well above horizon
        "left":   (0.7, _HEAD_HORIZON_BIAS),       # horizon, yaw left
        "right":  (-0.7, _HEAD_HORIZON_BIAS),      # horizon, yaw right
    }

    def _do_look(self, direction: str) -> None:
        """Update the persistent head/neck pose targets. Body stays still.
        The actual motor command is applied in _apply_state's tail so the
        pose survives plan expiry (otherwise the head snaps back to
        walkready the moment we transition to IDLE/STOP).
        """
        neck_t, head_t = self._LOOK_PRESETS.get(direction, (0.0, 0.0))
        self._neck_target = _clamp(
            neck_t,
            self.robot.min_motor_position(MOTOR_NECK),
            self.robot.max_motor_position(MOTOR_NECK),
        )
        self._head_target = _clamp(
            head_t,
            self.robot.min_motor_position(MOTOR_HEAD),
            self.robot.max_motor_position(MOTOR_HEAD),
        )
        self.robot.set_eye_led(LED_LOST)
        # Keep gait quiet so the body doesn't drift while only the head moves.
        self.robot.set_x_amplitude(0.0)
        self.robot.set_y_amplitude(0.0)
        self.robot.set_a_amplitude(0.0)
        self.robot.gait_step()

    def _do_celebrate(self) -> None:
        self.robot.set_eye_led(LED_CELEBRATE)
        self.robot.gait_stop()
        self.robot.play_motion(MOTION_HELLO)
        self.robot.play_motion(MOTION_WALKREADY)
        self.robot.gait_start()
        self._head_target = 0.0
        self._neck_target = 0.0
        self.state = State.IDLE
        self.plan = None  # one-shot

    # -------- Helpers --------
    def _track_and_walk(self, bx: float, by: float) -> None:
        # Integrate ball position into head pose (parity with Soccer.cpp).
        x = HEAD_GAIN * bx + self._px
        y = HEAD_GAIN * by + self._py
        self._px = x
        self._py = y
        neck = _clamp(
            -x,
            self.robot.min_motor_position(MOTOR_NECK),
            self.robot.max_motor_position(MOTOR_NECK),
        )
        head = _clamp(
            -y,
            self.robot.min_motor_position(MOTOR_HEAD),
            self.robot.max_motor_position(MOTOR_HEAD),
        )
        # Speed: far ball → fast walk; close ball → slow.
        self.robot.set_x_amplitude(1.0 if y < 0.1 else 0.5)
        self.robot.set_a_amplitude(neck)
        self.robot.gait_step()
        self.robot.set_motor_position(MOTOR_NECK, neck)
        self.robot.set_motor_position(MOTOR_HEAD, head)

    def _kick(self, side: str) -> None:
        """One-shot kick. Plays the motion once, then transitions to IDLE
        so the FSM requests a fresh snapshot for the next plan.
        """
        self.robot.gait_stop()
        self.robot.wait_ms(500)
        self.robot.set_eye_led(LED_OK)
        if side == "left":
            self.robot.play_motion(MOTION_KICK_LEFT)
        else:
            self.robot.play_motion(MOTION_KICK_RIGHT)
        self.robot.play_motion(MOTION_WALKREADY)
        self.robot.gait_start()
        self._px = 0.0
        self._py = 0.0
        # WALKREADY resets the head — sync our persistent targets so the
        # tail of _apply_state doesn't immediately move it back.
        self._head_target = 0.0
        self._neck_target = 0.0
        # CRITICAL: clear plan AND state so we don't re-enter _kick on the
        # next step. State must go to IDLE so the snapshot loop can fire.
        self.state = State.IDLE
        self.plan = None

    @staticmethod
    def _infer_retreat_side(snapshot: "Snapshot") -> str:
        ls = snapshot.line.side
        if ls == "left":
            return "right"
        if ls == "right":
            return "left"
        return "back"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
