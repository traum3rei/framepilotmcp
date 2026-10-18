"""FramePilot LangGraph agent: iterative locally, quota-conscious in the cloud.

Ollama can call tools repeatedly, inspecting the result after each step.
Cloud providers use exactly one model request and at most one MCP tool invocation.
"""

import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
load_dotenv(HERE / ".env")


def create_model(provider: str):
    if provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(
            model=os.getenv("OLLAMA_MODEL", "qwen3:4b-instruct"),
            temperature=0,
        )
    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")):
        raise RuntimeError("Set GOOGLE_API_KEY in agent/.env (or use --provider ollama).")
    from langchain_google_genai import ChatGoogleGenerativeAI
    return ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"),
        temperature=0,
        max_retries=0,  # minimize requests when quotas are low
    )


def display_content(content) -> str:
    """Avoid dumping base64 image data into the terminal."""
    if isinstance(content, str):
        return content[:1500]
    if isinstance(content, list):
        lines = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                lines.append(str(block.get("text", ""))[:1500])
            elif isinstance(block, dict) and block.get("type") in ("image", "image_url"):
                lines.append("[Bilddaten ausgelassen; Preview liegt unter runtime/preview.png]")
        return "\n".join(lines) or "[Nichttextuelles Ergebnis]"
    return str(content)[:1500]


async def main() -> None:
    parser = argparse.ArgumentParser(description="FramePilot: iterative Ollama, one-shot cloud agent")
    parser.add_argument("prompt", help="One instruction for FramePilot")
    parser.add_argument("--provider", choices=["gemini", "ollama"], default="gemini")
    parser.add_argument(
        "--allow",
        default="inspect_scene",
        help="Comma-separated MCP tool allowlist (default: inspect_scene)",
    )
    args = parser.parse_args()

    tsx = ROOT / "node_modules" / ".bin" / "tsx"
    server = ROOT / "src" / "server.ts"
    if not tsx.is_file() or not server.is_file():
        parser.error("FramePilot not found. Place agent/ in FramePilot root and run npm install.")

    requested = {name.strip() for name in args.allow.split(",") if name.strip()}
    if not requested:
        parser.error("--allow requires at least one MCP tool name")

    # Use the already-installed local tsx binary: no npx network lookup.
    client = MultiServerMCPClient({
        "framepilot": {
            "transport": "stdio",
            "command": str(tsx),
            "args": [str(server)],
            "cwd": str(ROOT),
        }
    })
    available = await client.get_tools()
    available_names = {tool.name for tool in available}
    unknown = requested - available_names
    if unknown:
        raise ValueError(f"Unknown tools: {sorted(unknown)}. Available: {sorted(available_names)}")
    allowed_tools = [tool for tool in available if tool.name in requested]

    local_mode = args.provider == "ollama"
    llm = create_model(args.provider).bind_tools(allowed_tools)

    async def model_step(state: MessagesState):
        response = await llm.ainvoke(state["messages"])
        # Run only one tool per turn to avoid concurrent Blender edits.
        # Ollama may call more tools in *subsequent* turns.
        if len(response.tool_calls) > 1:
            response = response.model_copy(update={"tool_calls": response.tool_calls[:1]})
        return {"messages": [response]}

    graph = StateGraph(MessagesState)
    graph.add_node("model", model_step)
    graph.add_node("tools", ToolNode(allowed_tools))
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", tools_condition)
    # The local model gets the tool result and decides the next step.
    # Cloud mode stops after one tool execution, without another LLM request.
    graph.add_edge("tools", "model" if local_mode else END)
    agent = graph.compile()

    common_instructions = (
        "You are FramePilot, a Blender assistant. You may ONLY use provided tools. "
        "Do not render unless explicitly asked. Do not invent object names or scene coordinates. "
        "Avoid destructive changes that were not requested."
    )
    if local_mode:
        policy = (
            "You may make multiple sequential tool calls to finish the user's task. "
            "Inspect the scene when you need positions or object names. "
            "After each tool result, decide whether another tool is needed. "
            "When the task is complete, respond normally WITHOUT calling another tool. "
            "Do not repeat calls that have already succeeded without a reason."
        )
    else:
        policy = (
            "You have a strict budget of ONE model request and at most ONE tool call. "
            "If the task needs more steps, perform only the first safe step."
        )

    result = await agent.ainvoke(
        {"messages": [
            SystemMessage(content=common_instructions + " " + policy),
            HumanMessage(content=args.prompt),
        ]},
        # A high recursion limit is only an emergency stop for a stuck local agent.
        # Each tool loop uses approximately two graph steps. No low tool-call quota.
        config={"recursion_limit": 1000 if local_mode else 4},
    )
    for message in result["messages"][2:]:
        if getattr(message, "tool_calls", None):
            call = message.tool_calls[0]
            print(f"MCP tool: {call['name']}({call['args']})")
        elif message.type == "tool":
            print("Tool result:", display_content(message.content))
        elif message.type == "ai":
            print("Agent:", display_content(message.content))


if __name__ == "__main__":
    asyncio.run(main())
