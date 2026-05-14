"""Sanity-check that the soccerv3 native extension built and is importable.

Run after `make build`:
    .venv/bin/python check.py

Does NOT instantiate Soccerv3Native (that needs a live Webots session).
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    try:
        import soccerv3_native
    except ImportError as exc:
        print(f"[FAIL] cannot import soccerv3_native: {exc}")
        print("       did you run 'make build'?")
        return 1

    print(f"[OK] soccerv3_native imported from {soccerv3_native.__file__}")

    expected = ("Soccerv3Native", "NMOTORS", "MOTOR_NECK", "MOTOR_HEAD")
    missing = [name for name in expected if not hasattr(soccerv3_native, name)]
    if missing:
        print(f"[FAIL] missing symbols: {missing}")
        return 1

    cls = soccerv3_native.Soccerv3Native
    new_methods = ("get_camera_frame", "camera_width", "camera_height")
    missing_methods = [m for m in new_methods if not hasattr(cls, m)]
    if missing_methods:
        print(f"[FAIL] Soccerv3Native is missing methods: {missing_methods}")
        return 1

    print(f"[OK] symbols present: {', '.join(expected)}")
    print(f"[OK] new camera methods present: {', '.join(new_methods)}")
    print(f"[OK] NMOTORS={soccerv3_native.NMOTORS}, "
          f"NECK={soccerv3_native.MOTOR_NECK}, HEAD={soccerv3_native.MOTOR_HEAD}")

    # --- vision.py: synthetic-frame round-trip test (no Webots required) ---
    try:
        import numpy as np
        import vision
    except ImportError as exc:
        print(f"[FAIL] cannot import vision/numpy: {exc}")
        return 1

    # Build a 64x48 BGRA frame: yellow rectangle in upper half,
    # white strip across the bottom (line). No orange (ball uses native manager).
    h, w = 48, 64
    frame = np.zeros((h, w, 4), dtype=np.uint8)
    frame[:, :, 3] = 255                               # alpha
    frame[5:15, 20:44] = (0, 255, 255, 255)            # BGRA: yellow region
    frame[h - 8:, :] = (255, 255, 255, 255)            # BGRA: white bottom band

    rgb = vision._bgra_buf_to_rgb(frame.tobytes(), w, h)
    hsv = vision._rgb_to_hsv(rgb)
    goal = vision._detect_goal_yellow(hsv)
    line = vision._detect_lines(hsv)

    if not goal.visible:
        print(f"[FAIL] vision did not detect synthetic yellow goal: {goal}")
        return 1
    if not line.near:
        print(f"[FAIL] vision did not detect synthetic white line: {line}")
        return 1
    print(f"[OK] vision goal detection: x={goal.x:+.2f} y={goal.y:+.2f} px={goal.pixels}")
    print(f"[OK] vision line detection: side={line.side} coverage={line.coverage:.2f}")

    print("All checks passed. Open the Webots world and select the soccerv3 controller.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
