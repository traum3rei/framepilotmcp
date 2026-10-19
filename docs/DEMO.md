# Reproducing a FramePilot demo
## Preparation

1. Open a *copy* of a `.blend` scene with an active camera and at least one editable light.
2. Enable the FramePilot Live add-on in Blender's 3D Viewport N sidebar and click **Start Live Bridge**.
3. Start Ollama locally and ensure a vision-capable model is installed, e.g. `ollama pull qwen3.5:4b`.
4. From the repository root, activate the Python environment with `source agent/.venv/bin/activate`.

## Step 1: Verify the live connection without token use

```bash
python agent/v1.py --inspect
```

Confirm that the output references the expected `.blend` file, camera, objects and existing lights. This reads through MCP but does not call the LLM or change the scene.

## Step 2: Plan without editing

```bash
python agent/v1.py \
  "Make this lighting more atmospheric without changing the composition." \
  --model qwen3.5:4b
```

The output should show the Director's selected specialist and a proposed action. This *dry run* does not render or edit the scene.

## Step 3: Compare baseline and candidate

```bash
python agent/v1.py \
  "Preserve detail on the main subject, keep the mood dark and cinematic, and avoid blown highlights." \
  --model qwen3.5:4b \
  --vision-model qwen3.5:4b \
  --apply --attempts 2
```

This captures a baseline (A), modifies the scene, renders a candidate (B), asks the vision model to compare them and keeps the preferred settings. Review the result in Blender before manually saving.

## Outputs

```text
runtime/v1_runs/<UTC timestamp>/
├── baseline.png
├── attempt_01.png
├── attempt_02.png       # if a second attempt was made
└── evaluation.json
```

`evaluation.json` contains the request, selected image, reviewer outcomes and selected camera/light state. The whole `runtime/` directory is intentionally gitignored; these files are local evidence of an individual test, not committed fixtures.

## Observations from local testing

- The v1 workflow successfully connected to a separate, user-created Blender scene and read its camera, object inventory and lights.
- A live comparison produced a reviewer verdict selecting a more atmospheric lighting candidate in one run.
- In another run, the reviewer preferred the original image because the candidate overexposed the subject. This is a useful **negative** example: the reviewer can reject a proposed change instead of always endorsing it.

These are observations from local experiments, not a statistically validated quality benchmark. Reviewers can make incorrect choices; keep the before/after images and human assessment for any public demonstration.

## Portfolio screenshots (optional)

To show FramePilot in a portfolio, include **only scenes and render images you have permission to publish**. A helpful set is:

- Screenshot of the FramePilot Live panel in Blender.
- Baseline and attempted candidate render with the same camera dimensions.
- Short terminal excerpt showing `[Vision Reviewer] Chose A` or `[Vision Reviewer] Chose B` and the restored state.
- A redacted `evaluation.json` snippet (remove local paths, usernames, secrets and nonpublic scene details).

The repository does not need to contain the `.blend` file or model weights. No example image is embedded here until a publicly shareable example is selected.
