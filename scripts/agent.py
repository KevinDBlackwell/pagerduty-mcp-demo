"""Ask Claude a question and let it answer with the MCP tools.

Starts the server over stdio, passes its tool list to Claude, and runs the tool-use loop.

    pip install anthropic
    export ANTHROPIC_API_KEY=...  PAGERDUTY_API_TOKEN=...
    python scripts/agent.py "Who's on call for Payments right now?"
    python scripts/agent.py --offline "Any open high-urgency incidents? Has anyone acknowledged them?"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MAX_TURNS = 8  # stop runaway tool loops


async def ask(question: str, offline: bool, verbose: bool) -> str:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    if offline:
        env["PD_MCP_FAKE"] = "1"
    params = StdioServerParameters(command=sys.executable, args=["-m", "pd_mcp.server"], env=env, cwd=str(ROOT))

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            listed = await session.list_tools()
            tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                     for t in listed.tools]
            system = (init.instructions or "") + "\nAnswer briefly. Cite incident numbers and people by name."

            llm = Anthropic()
            messages = [{"role": "user", "content": question}]
            for _ in range(MAX_TURNS):
                resp = llm.messages.create(model=MODEL, max_tokens=1500, system=system,
                                           tools=tools, messages=messages)
                messages.append({"role": "assistant", "content": resp.content})
                if resp.stop_reason != "tool_use":
                    return "".join(b.text for b in resp.content if b.type == "text")

                results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    if verbose:
                        print(f"-> {block.name}({json.dumps(block.input)})", file=sys.stderr)
                    out = await session.call_tool(block.name, block.input)
                    text = "\n".join(c.text for c in out.content if getattr(c, "text", None))
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": text, "is_error": bool(out.is_error)})
                messages.append({"role": "user", "content": results})
            return "Stopped: too many tool calls without an answer."


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--offline", action="store_true", help="use the in-memory fake PagerDuty")
    ap.add_argument("-q", "--quiet", action="store_true", help="hide the tool calls")
    a = ap.parse_args()
    print(asyncio.run(ask(a.question, a.offline, not a.quiet)))
