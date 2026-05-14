"""Robot-side vision processing — L1.5 of the 3T architecture.

Consumes the raw BGRA camera buffer from soccerv3_native and produces a small
dict of semantic features that L2 (the FSM) packages into a world snapshot for
L3 (the LLM agent).

No OpenCV dependency — pure numpy. The thresholds below are tuned for the
ROBOTIS-OP2 Webots demo world (orange ball, yellow goal posts, white field
lines) and may need adjustment for other worlds.

Debugging: every snapshot also dumps the current camera frame as PPM at
/tmp/soccerv3_cam.ppm — open in macOS Preview to see exactly what the robot
sees. Disable by setting the env var SOCCERV3_NO_CAM_DUMP=1.
"""

from __future__ import annotations

import os

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from soccerv3_native import Soccerv3Native


# ---------- HSV thresholds (degrees / 0-1 / 0-1) ----------
# White (low saturation, high value) — used both for field lines AND for the
# goal frame/netting. We disambiguate by frame region: lines live in the
# bottom band, the goal lives in the upper portion of the frame.
WHITE_MAX_SAT = 0.20
WHITE_MIN_VAL = 0.75

# Bottom band (near the robot's feet) where line proximity matters.
NEAR_LINE_BAND_RATIO = 0.30   # bottom 30% of the frame
NEAR_LINE_PIXEL_RATIO = 0.08  # white pixels >= 8% of band → "near line"

# Goal detection: scan the UPPER portion of the frame (above the line band)
# for white pixels. The goal is a tall vertical structure that occupies a
# LARGE chunk of the frame when in view; field lines and the robot's own
# white feet have far fewer connected white pixels.
GOAL_BAND_RATIO = 0.55        # consider the upper 55% of the frame only
GOAL_MIN_PIXELS = 2500        # ~3% of a 320x240 frame — filters out lines


# ---------- Public types ----------

@dataclass
class BallObservation:
    visible: bool = False
    x: float = 0.0
    y: float = 0.0
    distance_est: float = 0.0  # rough proxy: 1.0 = far (top of frame), 0.0 = close (bottom)


@dataclass
class GoalObservation:
    visible: bool = False
    x: float = 0.0
    y: float = 0.0
    pixels: int = 0


@dataclass
class LineObservation:
    near: bool = False
    side: Optional[str] = None  # "left" | "center" | "right" | None
    coverage: float = 0.0       # white pixel ratio in the bottom band [0, 1]


@dataclass
class Snapshot:
    ball: BallObservation = field(default_factory=BallObservation)
    goal: GoalObservation = field(default_factory=GoalObservation)
    line: LineObservation = field(default_factory=LineObservation)
    # Raw RGB array (HxWx3 uint8). Not part of the textual snapshot, but
    # the FSM forwards it to the MCP bus so Claude can SEE the frame.
    _rgb: Optional[np.ndarray] = field(default=None, repr=False)

    def as_dict(self) -> dict:
        return {
            "ball": self.ball.__dict__,
            "goal": self.goal.__dict__,
            "line": self.line.__dict__,
        }

    def as_text(self) -> str:
        """Compact text representation for the LLM. One line, key:value pairs."""
        b, g, l = self.ball, self.goal, self.line
        parts = [
            f"ball:{'yes' if b.visible else 'no'}",
            f"ball_x:{b.x:+.2f}",
            f"ball_y:{b.y:+.2f}",
            f"ball_dist_est:{b.distance_est:.2f}",
            f"goal:{'yes' if g.visible else 'no'}",
            f"goal_x:{g.x:+.2f}",
            f"goal_y:{g.y:+.2f}",
            f"near_line:{'yes' if l.near else 'no'}",
            f"line_side:{l.side or 'none'}",
        ]
        return " ".join(parts)


# ---------- Internals ----------

def _bgra_buf_to_rgb(buf: bytes, width: int, height: int) -> np.ndarray:
    """Convert Webots' BGRA buffer to an HxWx3 uint8 RGB array."""
    arr = np.frombuffer(buf, dtype=np.uint8)
    if arr.size != width * height * 4:
        raise ValueError(
            f"camera buffer size {arr.size} does not match {width}x{height}x4"
        )
    bgra = arr.reshape(height, width, 4)
    # B,G,R,A → R,G,B
    return bgra[:, :, 2::-1]


def _rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    """Vectorized RGB→HSV. Output: H in [0,360), S in [0,1], V in [0,1]."""
    rgb_f = rgb.astype(np.float32) / 255.0
    r, g, b = rgb_f[..., 0], rgb_f[..., 1], rgb_f[..., 2]

    cmax = np.max(rgb_f, axis=-1)
    cmin = np.min(rgb_f, axis=-1)
    delta = cmax - cmin

    # Hue (avoid divide-by-zero)
    safe_delta = np.where(delta == 0, 1.0, delta)
    h_r = ((g - b) / safe_delta) % 6.0
    h_g = (b - r) / safe_delta + 2.0
    h_b = (r - g) / safe_delta + 4.0
    h = np.zeros_like(cmax)
    h = np.where(cmax == r, h_r, h)
    h = np.where(cmax == g, h_g, h)
    h = np.where(cmax == b, h_b, h)
    h = (h * 60.0) % 360.0
    h[delta == 0] = 0.0

    s = np.where(cmax > 0, delta / np.where(cmax == 0, 1.0, cmax), 0.0)
    v = cmax
    return np.stack([h, s, v], axis=-1)


def _detect_goal_white(hsv: np.ndarray) -> GoalObservation:
    """Detect the goal as a white structure in the upper portion of the frame.

    Standard RoboCup soccer goals are white (frame + netting). Field lines are
    also white but live near the ground, so we only consider the UPPER
    GOAL_BAND_RATIO of the frame to avoid confusion.
    """
    height, width = hsv.shape[:2]
    band_end = int(height * GOAL_BAND_RATIO)
    band = hsv[:band_end, :, :]

    s, v = band[..., 1], band[..., 2]
    mask = (s <= WHITE_MAX_SAT) & (v >= WHITE_MIN_VAL)
    pixel_count = int(mask.sum())
    if pixel_count < GOAL_MIN_PIXELS:
        return GoalObservation()

    ys, xs = np.nonzero(mask)
    # ys are indices into the BAND, but x is full-width: convert both to
    # fractions of the full frame so x/y are comparable to ball coords.
    cx = float(xs.mean()) / max(width, 1)         # 0..1 left→right
    cy = float(ys.mean()) / max(height, 1)        # 0..1 top→bottom
    return GoalObservation(
        visible=True,
        x=2.0 * cx - 1.0,
        y=2.0 * cy - 1.0,
        pixels=pixel_count,
    )


def _detect_lines(hsv: np.ndarray) -> LineObservation:
    height = hsv.shape[0]
    band_start = int(height * (1.0 - NEAR_LINE_BAND_RATIO))
    band = hsv[band_start:, :, :]
    s, v = band[..., 1], band[..., 2]
    mask = (s <= WHITE_MAX_SAT) & (v >= WHITE_MIN_VAL)

    band_pixels = mask.size
    white_pixels = int(mask.sum())
    coverage = white_pixels / max(band_pixels, 1)

    if coverage < NEAR_LINE_PIXEL_RATIO:
        return LineObservation(near=False, side=None, coverage=coverage)

    # Determine which side has more white density.
    band_w = mask.shape[1]
    third = band_w // 3
    left = int(mask[:, :third].sum())
    center = int(mask[:, third:2 * third].sum())
    right = int(mask[:, 2 * third:].sum())
    side = max(("left", left), ("center", center), ("right", right), key=lambda kv: kv[1])[0]
    return LineObservation(near=True, side=side, coverage=coverage)


# ---------- Public API ----------

def snapshot(robot: "Soccerv3Native") -> Snapshot:
    """Build a feature snapshot from one camera frame.

    Reuses the ROBOTIS-OP2 vision manager for the orange ball (so we keep
    parity with soccerv2's behavior) and adds yellow-goal / white-line
    detection on top via numpy.
    """
    snap = Snapshot()

    # --- Ball: trust the C++ manager, same thresholds as soccerv2.
    found, bx, by = robot.get_ball_center()
    if found:
        # Closer to the bottom of the frame -> closer to the robot.
        # by is in [-1, 1] with -1 = top, +1 = bottom; map to [0, 1] distance estimate
        # where 0 = closest, 1 = farthest. (1 - (by + 1) / 2)
        snap.ball = BallObservation(
            visible=True,
            x=bx,
            y=by,
            distance_est=max(0.0, min(1.0, 1.0 - (by + 1.0) / 2.0)),
        )

    # --- Goal + lines: process the raw BGRA buffer with HSV.
    width = robot.camera_width()
    height = robot.camera_height()
    if width > 0 and height > 0:
        buf = robot.get_camera_frame()
        if buf:
            rgb = _bgra_buf_to_rgb(buf, width, height)
            hsv = _rgb_to_hsv(rgb)
            snap.goal = _detect_goal_white(hsv)
            snap.line = _detect_lines(hsv)
            # Stash the raw RGB so callers (FSM) can publish it to the MCP
            # bus for Claude to view directly.
            snap._rgb = rgb
            if not os.environ.get("SOCCERV3_NO_CAM_DUMP"):
                _dump_ppm(rgb, "/tmp/soccerv3_cam.ppm")

    return snap


def _dump_ppm(rgb: np.ndarray, path: str) -> None:
    """Write an RGB array as a binary PPM (P6). macOS Preview opens it."""
    h, w, _ = rgb.shape
    try:
        with open(path, "wb") as f:
            f.write(f"P6\n{w} {h}\n255\n".encode("ascii"))
            f.write(rgb.astype(np.uint8).tobytes())
    except OSError:
        pass  # disk full / read-only — non-fatal
