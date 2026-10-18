# FramePilot – LangGraph agent add-on

This is **an additive extension**. Copy the `agent/` directory into your **existing** FramePilot project root, alongside `src/` and `scripts/`. It does not overwrite your MCP server or your own `set_light_energy` work.

## Install (Mac)

```bash
cd /path/to/YOUR/framepilot
python3 -m venv agent/.venv
source agent/.venv/bin/activate
pip install -r agent/requirements.txt
cp agent/.env.example agent/.env
```

Edit `agent/.env` and enter your Gemini API key. Don't check it into Git. Or use local Ollama (`ollama pull qwen3:4b-instruct`; use `--provider ollama`). You should already have run `npm install` in your FramePilot project.

## One quick test

```bash
python agent/agent.py "Inspect the Blender scene and list its objects."
```

The default allowlist includes ONLY `inspect_scene`. The agent makes **one LLM request** and at most **one MCP tool call**. It does not retry or render. It should print the MCP tool call and the returned scene data (truncated to 1,500 chars).

To allow *one* camera change, use:

```bash
python agent/agent.py "Set camera position [0,-12,3], aim at [0,0,2], lens 50mm" --allow set_camera
```

If you implemented your additional light tool:

```bash
python agent/agent.py "Set 'Warm eye light' energy to 200" --allow set_light_energy
```

To test a preview (may take time):

```bash
python agent/agent.py "Render one preview" --allow render_preview
```

If you prefer local inference, add `--provider ollama` to a command above after pulling the model and starting Ollama.

## Why this is LangGraph

```
START -> model -> tools? -> END
                 \-----> END (if no tool)
```

`MultiServerMCPClient` discovers tools from your TypeScript MCP server and converts them to LangChain tools. `model.bind_tools` gives the LLM the tool schemas. `StateGraph(MessagesState)` routes the tool call to `ToolNode`. The tool node goes directly to `END`, intentionally preventing extra model requests and automatic loops.

For a true multi-step visual director, the next iteration can add an approval node and permit model -> tools -> model with an explicit budget, plus image evaluation. This version deliberately limits token usage and avoids unapproved repeated changes.

## Limitations

- Requires Python and a working FramePilot Node setup on the same computer.
- A model may answer without invoking an available tool; this is not necessarily an integration failure. For the first test, explicitly ask it to *use* `inspect_scene`.
- Gemini API requests may be billable depending on your account. Codex quota is unaffected because this is a separate agent.
- The project has passed Python syntax validation only; full integration requires your installed packages, local Blender and an LLM endpoint.
