# FramePilot v0.6 — Lighting Agent (non-destructive add-on)

This update ONLY ADDS `agent/lighting_agent.py`, `agent/lighting_checks.py`,
`agent/tests/test_lighting_checks.py`, and an optional lighting setup script
in `extras/preview_corridor_lights.py`. It does not replace or patch your current
`agent/orchestrator.py`, `src/server.ts`, Blender add-on, or `.env`.

## Why

Your selected shot shows the pillars, but the corridors remain underexposed.
Further moving the camera does not solve that lighting problem. This separate
LangGraph workflow keeps the current camera unchanged and tunes the existing
**FramePilot-managed corridor lights**. It uses the same local MCP connection.

## Set up the three demo lights (ONE TIME, inside open Blender)

If these three lights do NOT exist in the Blender Outliner:
- `FramePilot / Blue corridor fill`
- `FramePilot / Warm corridor fill`
- `FramePilot / Back pillar rim`

Go to **Scripting** → **Open** `scripts/preview_corridor_lights.py` → **Run Script**.
If that file is missing, use the included `extras/preview_corridor_lights.py`.
The script checks it is the FramePilot demo, creates/updates these named lights,
and does NOT save the `.blend` file. If you already ran it, skip this step.

The agent requires a working MCP tool `set_light_energy` taking
`light_name` and `energy`, alongside `inspect_scene` and `render_preview`.
Your previous FramePilot version added this tool. If missing, it stops cleanly
with instructions rather than editing any other objects.

## Install

Extract this ZIP **into the root of your existing FramePilot project**.
Place `agent/` next to your existing `agent/orchestrator.py`.
Do not overwrite your existing `agent/.env`.

```bash
source agent/.venv/bin/activate
python -m unittest discover -s agent/tests -v
```

## Minimal first test (one model plan, no scene edits)

```bash
python agent/lighting_agent.py --model qwen3.5:4b
```

## Try one lighting adjustment and one A/B vision comparison

Start Ollama, open Blender, make sure **Start Live Bridge** is active, and leave
it at your selected camera. Then:

```bash
python agent/lighting_agent.py \
  "Make the existing pillars, walls and floor readable but keep deep, cinematic shadows and a prominent floating eye." \
  --model qwen3.5:4b \
  --vision-model qwen3.5:4b \
  --attempts 1 \
  --apply
```

`--attempts 1` renders **baseline + 1 candidate** and makes one visual comparison.
For an extra candidate, use `--attempts 2` (baseline + up to 2 candidates).

The agent uses a LangGraph pipeline:
`baseline → lighting designer → MCP set_light_energy → render → vision reviewer
→ [retry or restore best]`. It compares the REAL baseline PNG against each
new render using your LOCAL Ollama Vision model, then restores the best energy
settings. Changes remain **unsaved** in Blender. It does not call `set_camera`.

## Outputs

- `runtime/lighting_runs/<timestamp>/baseline.png`
- `runtime/lighting_runs/<timestamp>/lighting_01.png` (and `_02`, if requested)
- `runtime/lighting_runs/<timestamp>/lighting_evaluation.json`
- `runtime/preview.png` is restored from the selected saved image (not re-rendered).

## Safety / limitations

- The tool ONLY changes the three specifically named corridor lights.
  It does not touch the original `Warm eye light` or edit
  geometry, camera, or materials. No automatic saves are performed.
- Energies are limited to 50–4000 and new plans must meaningfully differ.
- If an error occurs, the agent attempts to restore the last best known
  settings; if Blender disconnected, it prints the initial values for manual
  restoration. Save a copy of your scene before experimenting.
- You must create the original area lights manually with the supplied script
  once. Changing energy alone cannot fix bad light placement. Later we can add
  safe MCP tools for area-light position/size/color.
- Image A/B assessment is subjective: Qwen 3.5 4B can incorrectly prefer a
  brighter but less cinematic image. Inspect the archived PNGs yourself.
- This is a **separate lighting subgraph**; it is not yet automatically routed
  from `orchestrator.py`. Keeping it separate protects your working camera
  pipeline while we validate lighting changes.
- Pure-Python logic and syntax were tested; this environment cannot execute a
  full Blender Live + Ollama run.
