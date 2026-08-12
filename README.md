# Brain Blocks — Navi Edition

Blockly-based visual programming for the **Navi** robot (more to come later).
Drag and drop blocks to control the robot over Wi-Fi, add voice interactions
(speak / listen / ask via the Web Speech API), and vision (hand landmarks,
gesture recognition, face landmarks via MediaPipe). Comes with various skills.

## Quick start

Serve the folder over HTTP — Web Speech, camera access, and the Navi gateway
require `http(s)`:

```bash
python3 -m http.server 8000
```

Then open `http://localhost:8000/brain_blocks_v2%201.html`.

## Features

- Full Blockly editor with Blocks / JSON / Code tabs, and a Publish tab for
  naming, tagging, and sharing programs (JSON round-trips metadata)
- Robot runtime with run highlighting, console, run log, device log, and
  telemetry views
- Wi-Fi gateway integration: scan / connect endpoints on the robot's hotspot,
  with trust-cert auto-retry
- Phone-side blocks: `ask`, `speak` (TTS), `hear` (STT), `playSound`
- Vision blocks built on MediaPipe: hand landmarks, gesture recognition, face
  landmarks
- Procedures with parameters, stop-aware loops, and run-safe defaults
- Skill library: playable voice games and demos (see `skills/`)

## Repository layout

| Path | What it is |
|---|---|
| `brain_blocks_v2 1.html` | Main app (self-contained editor + runtime) |
| `Brain Blocks · Navi Edition.html`, `brain_blocks_v2 mock.html` | Earlier builds |
| `blockly/` | Blockly v9.3.3 — vendored, unmodified (Apache-2.0) |
| `mediapipe/` | `@mediapipe/tasks-vision` 0.10.14 + `.task` models — vendored, unmodified (Apache-2.0) |
| `skills/` | Skill definitions (JSON), e.g. 20 Questions, Categories, Guess-the-number, Taboo (voice) |
| `tools/` | Build & validation scripts for skills (Python / Node) |

## Skills

Voice games and demos live in `skills/` as JSON. They are generated and
validated with the scripts in `tools/`:

```bash
python3 tools/build_games.py
node tools/validate_games.mjs
```

## License

Apache-2.0. Blockly (Google LLC) and MediaPipe / MediaPipe models (Google LLC)
are Apache-2.0 and are redistributed unmodified.
