"""One-shot LangGraph agent using FramePilot's existing TypeScript MCP server.

One LLM request and at most one MCP tool invocation per process. No autonomous loops.
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
    parser = argparse.ArgumentParser(description="Small, quota-conscious FramePilot LangGraph agent")
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

    llm = create_model(args.provider).bind_tools(allowed_tools)

    async def model_step(state: MessagesState):
        response = await llm.ainvoke(state["messages"])
        # Enforce one tool call even when the model requests several in parallel.
        if len(response.tool_calls) > 1:
            response = response.model_copy(update={"tool_calls": response.tool_calls[:1]})
        return {"messages": [response]}

    graph = StateGraph(MessagesState)
    graph.add_node("model", model_step)
    graph.add_node("tools", ToolNode(allowed_tools))
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", tools_condition)
    graph.add_edge("tools", END)  # no second LLM request / no autonomous retries
    agent = graph.compile()

    result = await agent.ainvoke({"messages": [
        SystemMessage(content=(
            "You are FramePilot, a Blender assistant. You may ONLY use provided tools. "
            "Choose no more than one tool. Do not request renders unless explicitly asked. "
            "If a task needs multiple tool calls, do only the first safe step. "
            "Do not invent Blender coordinates or object names."
        )),
        HumanMessage(content=args.prompt),
    ]})
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
