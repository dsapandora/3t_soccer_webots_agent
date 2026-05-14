# soccerv3 — Setup & Run Guide

Python equivalent of `soccerv2` (which mirrors `soccer/Soccer.cpp`). The walking
gait, motion playback and ball detection are delegated to a pybind11 extension
(`soccerv3_native`) that wraps the ROBOTIS-OP2 managers shipped with Webots.

## Requirements

- macOS / Linux (Windows untested)
- Webots installed
  - macOS: `/Applications/Webots.app`
  - Linux: `/usr/local/webots` (or set `WEBOTS_HOME`)
- Python 3.10+ on `PATH` (controller was tested with Python 3.13)
- A C++17 toolchain (clang on macOS, g++ on Linux)

## Build

From this directory:

```bash
make
```

This will:
1. Create a `.venv/` virtualenv inside this folder.
2. Install `pybind11`, `setuptools`, `wheel`.
3. Compile `soccerv3_native.cpp` against the Webots managers + robotis-op2 + CppController libs.
4. Drop the resulting `soccerv3_native.cpython-*.so` next to `soccerv3.py`.

Verify the build:

```bash
make check
```

Expected output:

```
[OK] soccerv3_native imported from <path>/soccerv3_native.cpython-313-darwin.so
[OK] symbols present: Soccerv3Native, NMOTORS, MOTOR_NECK, MOTOR_HEAD
[OK] NMOTORS=20, NECK=18, HEAD=19
All checks passed.
```

## Run inside Webots

1. Open Webots.
2. Open the world that previously ran `soccer` or `soccerv2` (typically
   `projects/webots/worlds/soccer.wbt`, or whichever world you use for the
   ROBOTIS OP2 demo).
3. Right-click on the robot node → Edit Robot → set the `controller` field
   to `soccerv3`.
4. Press **Play**.

Webots launches the controller using the interpreter declared in `runtime.ini`:

```
[python]
COMMAND = .venv/bin/python3
```

…which is the venv created by `make`. That interpreter has both `pybind11`
and the compiled `soccerv3_native` extension on its `sys.path` (the controller
inserts its own directory at startup).

## Expected behavior — should match soccerv2

When the world starts the robot should:
- print the demo banner to the Webots console
- speak the greeting via the speaker
- play the hello motion (page 24)
- start walking, scanning for the orange ball
- once found: turn LEDs blue, walk toward it, kick (left or right) when close
- if it loses the ball: turn LEDs red, spin in place, oscillate the head
- if it falls: detect via accelerometer and play the f_up / b_up motion

If any of these behaviors differ from `soccerv2`, the discrepancy is a bug in
`soccerv3.py` — the underlying managers are identical (same `libmanagers.dylib`).

## Troubleshooting

### `make check` shows `Library not loaded: @rpath/...`

The `soccerv3_native.so` rpaths point at the Webots root by default
(`/Applications/Webots.app` on macOS). If you have Webots installed elsewhere,
override:

```bash
WEBOTS_HOME=/path/to/Webots.app make
```

### Webots launches the controller but Python can't find `soccerv3_native`

Webots probably ignored `runtime.ini` and used a system Python that does not
have the extension on `sys.path`. Confirm `runtime.ini` exists in this folder
and the path `.venv/bin/python3` resolves. As a fallback you can replace the
relative path with an absolute one.

### Pipeline error / missing `webots/Robot.hpp` while building

Same root cause as the `soccer/`-`soccerv2/` Makefile saga: the macOS Webots
SDK requires `WEBOTS_HOME=/Applications/Webots.app` (NOT `.app/Contents`). The
`setup.py` here defaults to that already. See `../MAKEFILE_NOTES.md` for the
full explanation.

### `pybind11` not installed when running `setup.py` directly

Run `make` (which creates the venv first), or activate the venv manually:

```bash
source .venv/bin/activate
pip install pybind11
python setup.py build_ext --inplace
```

## Files

| File                  | Purpose                                                   |
|-----------------------|-----------------------------------------------------------|
| `soccerv3.py`         | Python controller — mirror of `Soccer.cpp::run()`         |
| `soccerv3_native.cpp` | pybind11 binding wrapping ROBOTIS-OP2 managers            |
| `setup.py`            | Builds the extension (links Webots libs, embeds rpaths)   |
| `Makefile`            | Orchestrates venv + setup.py                              |
| `requirements.txt`    | Python deps (`pybind11`)                                  |
| `runtime.ini`         | Tells Webots which Python interpreter to launch           |
| `config.ini`          | Walking parameters (read by `RobotisOp2GaitManager`)      |
| `check.py`            | Sanity-check that the extension built and is importable   |

## Phase 2 — RocketRide multi-agent integration

`soccerv3.py` now follows a 3T architecture: reactive skills in C++,
perception in Python, sequencer FSM in Python, and a Claude-backed
RocketRide pipeline as the deliberative planner.

```
L1   soccerv3_native    skills (gait, motion, vision manager)
L1.5 vision.py          numpy HSV → ball/goal/line features
L2   fsm.py             state machine + reactive overrides
L3   strategy.pipe      multi-agent (strategist → tactician) on RocketRide
```

### Required env vars

Copy `.env.example` to `.env` and fill in:

```bash
ROCKETRIDE_URI=https://cloud.rocketride.ai
ROCKETRIDE_APIKEY=…
ROCKETRIDE_ANTHROPIC_KEY=sk-ant-api03-…
```

The Python SDK auto-reads `.env` (in this folder) on `RocketRideClient()`.
`${ROCKETRIDE_*}` placeholders inside `strategy.pipe` are substituted before
the JSON is sent to the server. Real keys never enter the pipe file.

### Pipeline

`strategy.pipe` declares 8 components — all stock RocketRide nodes (no
custom nodes needed in `rocketride-server`):

- `chat_1` — source, receives the world snapshot string each iteration
- `agent_strategist_1` — picks gameplay mode (ATTACK_GOAL/DEFEND_AREA/…)
- `agent_tactician_1` — invoked by the strategist as a sub-agent tool;
  returns a concrete action JSON
- `llm_anthropic_*` (×2) — Claude Sonnet 4.6, one per agent
- `memory_internal_*` (×2) — keyed memory per agent
- `response_answers_1` — JSON plan back to the controller

The agents are instructed to return single-line JSON:

```
{"action":"ATTACK_GOAL","params":{"side":"right"},"duration_ms":4000,"rationale":"ball close, goal visible right"}
```

### Run

1. `make` — builds the native extension (run once or after C++ edits)
2. `cp .env.example .env` and fill in your keys
3. Open Webots → reload world → press **Play**

The controller will:
- Print `RocketRide bridge ready.` if the pipeline came up
- Or print `RocketRide bridge unavailable — running in reactive-only mode.`
  if `.env` is missing or the server is not reachable. The robot still
  plays (search/recover/fall/line) but never gets LLM plans.

### Behavior knobs (in `fsm.py`)

- `SNAPSHOT_MIN_INTERVAL_S = 0.4` — min gap between snapshots sent to L3
- `KICK_TRIGGER_Y = 0.35` — ball-Y threshold to switch from approach to kick
- `ACC_FALL_STEPS = 20` — confirmation samples before triggering a recovery

Reactive overrides are intentionally simple and never wait on the LLM:
- Fall (face-down/back-down) → recovery motion immediately
- `near_line:yes` → AVOID_LINE retreat immediately

### Troubleshooting

**"RocketRide bridge unavailable"**:
- Check `.env` exists in this folder with valid `ROCKETRIDE_URI` and `ROCKETRIDE_APIKEY`
- Confirm the server is reachable (try a `curl` to `${ROCKETRIDE_URI}/health` or similar)
- Look at the controller's stderr; the bridge logs the underlying error

**LLM plan never arrives**:
- Increase logging: `logging.basicConfig(level=logging.DEBUG)` in `soccerv3.py`
- Check the agent answer in logs — if Claude returns prose instead of JSON,
  tweak the `instructions` in `strategy.pipe`

**"Pipeline Already Running"**:
- The bridge always uses `use_existing=True` so this should not happen.
  If it does, the pipe was started by another process with `use_existing=False` —
  restart that process or use the RocketRide CLI to terminate the running pipe.
