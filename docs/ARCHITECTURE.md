# Architecture and design decisions

FramePilot v1 is a local prototype combining a **TypeScript MCP server**, a **Blender Python add-on**, and a **Python LangGraph workflow**. The components are separated so a host such as Codex may consume individual tools, while the local agent can use the same MCP interface for a multistep workflow.

## Component responsibilities

| Component | Responsibility |
| --- | --- |
| `src/server.ts` | MCP server over stdio, general tool registration, serializes operations, exposes legacy headless tools |
| `src/v1_tools.ts` | Schema-validated, generic Blender Live tools for object/light queries and controlled changes |
| `src/live.ts` | Localhost socket client; validates connection information and sends commands with a token |
| `addons/framepilot_live.py` | Receives commands, executes `bpy` operations on Blender's main thread via a timer, and returns structured results |
| `agent/v1.py` | LangGraph state machine with Director, Camera/Lighting specialists, render evaluation and best-state restoration |
| `agent/v1_core.py` | Deterministic validation of proposal JSON, camera vectors, light parameters and evaluation responses |

## Data flow

1. With Blender already running, a user activates the **FramePilot Live** add-on. It creates a loopback connection described in a token-bearing file inside `runtime/`.
2. The Python agent starts a TypeScript MCP server subprocess via `MultiServerMCPClient` and discovers available tools.
3. `inspect_scene`, `list_scene_objects` and `list_lights` read the **current open Blender scene**. The v1 agent requires `mode=live` and refuses a headless fallback.
4. The **Director** selects a Camera or Lighting specialist from the request and scene summary.
5. A specialist returns structured JSON. Python validates the proposal; the Camera specialist targets scene coordinates, while the Lighting specialist changes properties of an existing named light.
6. In `--apply` mode, FramePilot captures a baseline preview, applies candidate changes through MCP, renders another preview and invokes a **vision reviewer** with both images.
7. The reviewer chooses A (existing best) or B (new candidate), returns qualitative feedback and can direct the next proposal to Camera or Lighting. The loop is capped by `--attempts` (1–5).
8. FramePilot records the selected candidate and attempts to restore its camera and light settings in Blender. It writes an `evaluation.json` record and leaves saving the `.blend` file to the user.

## Why multiple agents?

The roles represent distinct responsibilities within one LangGraph workflow:

- **Director:** interprets the desired visual effect and routes the request.
- **Camera specialist:** proposes camera location, target and focal length.
- **Lighting specialist:** proposes intensity/color (and area-light size) changes to an existing light.
- **Vision reviewer:** compares *actual preview images*, feeds back observed issues and chooses the preferred candidate.

This is a constrained multi-role implementation. It is not a fully general multi-agent system with arbitrary tool discovery or independent parallel workers. The model's judgments are advisory; Python validates the action schemas and manages state.

## Safeguards and boundaries

- **Read-only option:** `python agent/v1.py --inspect` does not invoke an LLM and cannot edit or render.
- **Explicit edit mode:** without `--apply`, v1 only produces a proposed plan; with `--apply`, scene changes and renders are permitted.
- **Validation:** TypeScript/Zod validates MCP arguments, while Python validates model proposals before tool calls.
- **Loop limit:** the v1 CLI restricts attempts to 1–5, preventing unbounded candidate rendering.
- **Best-known state:** the workflow tracks the last selected camera/light state and tries to restore it, including after most execution failures. This is best-effort and should not be treated as transactional rollback.
- **Local transport:** the live bridge binds to `127.0.0.1`, uses a randomly selected port and an authentication token; Blender's main thread executes `bpy` changes. The token file lives under the gitignored `runtime/` directory.
- **Controlled scope:** v1's automatic path does not create objects, edit geometry/materials or save the Blender file. A separate `create_light` MCP tool exists for explicit manual actions.

## Current limitations / next steps

1. **Image quality:** comparisons use small previews, which may miss fine highlight clipping, depth cues or shader detail. A preference from the vision model is not ground truth.
2. **Scene understanding:** object names and bounds do not fully convey what appears in the camera view, particularly for rigs, instances, shaders and occluded objects.
3. **Limited lighting actions:** the automatic agent changes one existing light per proposal. Exposure, color management and world settings are outside its scope.
4. **Error handling:** JSON validation detects malformed proposals, but local LLMs can still repeat unhelpful actions or choose incorrect artistic goals.
5. **Reproducibility:** model weights, Blender version, render engine and lighting/materials affect outputs. Preview images and `evaluation.json` should be retained with a demonstration.

## Testing

Use `npm run check` for TypeScript and `python -m unittest discover -s agent/tests -v` for local Python tests. End-to-end behavior must be verified in an open Blender session connected to the live bridge and a running local Ollama instance.
