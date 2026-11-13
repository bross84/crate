"""Model-agnostic runner: Crate acts as its own MCP client.

Starts the MCP servers listed in .mcp.json, offers their tools to any
OpenAI-compatible chat model (OpenRouter, OpenAI, Ollama, LM Studio, ...),
runs the tool calls the model asks for, and returns its final answer.

Used when CRATE_BACKEND=openai. The default backend is Claude Code.
"""
import asyncio
import json
import pathlib
import re
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import PaginatedRequestParams

MAX_TURNS = 80          # model round trips before giving up
MAX_RESULT_CHARS = 20000  # trim huge tool results so they don't flood the context


def load_servers(mcp_json: pathlib.Path):
    """Read the stdio MCP servers from a Claude-Code-style .mcp.json."""
    if not mcp_json.exists():
        raise RuntimeError(f"{mcp_json.name} not found. Copy .mcp.example.json and fill it in.")
    servers = json.loads(mcp_json.read_text()).get("mcpServers", {})
    if not servers:
        raise RuntimeError(f"No servers listed under mcpServers in {mcp_json.name}.")
    return {
        name: StdioServerParameters(
            command=cfg["command"], args=cfg.get("args", []), env=cfg.get("env"), cwd=mcp_json.parent
        )
        for name, cfg in servers.items()
        if cfg.get("command")  # stdio servers only
    }


async def list_all_tools(session: ClientSession):
    tools, cursor = [], None
    while True:
        page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
        tools += page.tools
        cursor = page.next_cursor
        if not cursor:
            return tools


def tool_name(server: str, tool: str):
    # OpenAI-style function names allow only [a-zA-Z0-9_-], up to 64 chars.
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"{server}__{tool}")[:64]


def result_text(result):
    parts = []
    for block in getattr(result, "content", None) or []:
        parts.append(block.text if getattr(block, "type", "") == "text" else f"[{block.type} content]")
    if not parts and getattr(result, "structured_content", None) is not None:
        parts.append(json.dumps(result.structured_content))
    text = "\n".join(parts) or "(no output)"
    if getattr(result, "is_error", False):
        text = f"Tool error: {text}"
    return text[:MAX_RESULT_CHARS]


async def run_agent(prompt: str, *, client, model: str, mcp_json: pathlib.Path, log=print):
    """Run one discovery prompt to completion and return the model's final text."""
    async with AsyncExitStack() as stack:
        routes, tools = {}, []
        for server, params in load_servers(mcp_json).items():
            try:
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                server_tools = await list_all_tools(session)
            except Exception as exc:
                raise RuntimeError(f"Couldn't start the {server} MCP server: {exc}") from exc
            for t in server_tools:
                name = tool_name(server, t.name)
                routes[name] = (session, t.name)
                tools.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": (t.description or "")[:1024],
                        "parameters": t.input_schema or {"type": "object", "properties": {}},
                    },
                })
            log(f"{server}: {len(server_tools)} tools")

        messages = [{"role": "user", "content": prompt}]
        for _ in range(MAX_TURNS):
            response = await client.chat.completions.create(model=model, messages=messages, tools=tools)
            if not response.choices:
                raise RuntimeError("The model returned no answer.")
            msg = response.choices[0].message
            calls = msg.tool_calls or []
            if not calls:
                return (msg.content or "").strip()

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                    for c in calls
                ],
            })
            for call in calls:
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": await run_tool(routes, call.function.name, call.function.arguments),
                })
        raise RuntimeError(f"Gave up after {MAX_TURNS} model turns without a final answer.")


async def run_tool(routes, name, raw_args):
    if name not in routes:
        return f"Tool error: there is no tool named {name}."
    try:
        args = json.loads(raw_args) if raw_args else {}
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except ValueError as exc:
        return f"Tool error: couldn't read the arguments as JSON ({exc})."
    session, real_name = routes[name]
    try:
        return result_text(await session.call_tool(real_name, args))
    except Exception as exc:
        return f"Tool error: {exc}"


def run_sync(prompt: str, *, base_url: str, api_key: str, model: str, mcp_json: pathlib.Path, timeout: float):
    from openai import AsyncOpenAI

    async def main():
        client = AsyncOpenAI(base_url=base_url or None, api_key=api_key or "none")
        return await asyncio.wait_for(
            run_agent(prompt, client=client, model=model, mcp_json=mcp_json), timeout
        )

    return asyncio.run(main())
