"""Scripted tool-call demo over real MCP (stdio), the same path an LLM client uses.

It plays the calls a model would make for the two example questions, plus the edge cases.

    python scripts/demo.py --offline        # no network: in-memory fake PagerDuty
    PAGERDUTY_API_TOKEN=... python scripts/demo.py --service Payments
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]


def show(title: str, result) -> dict:
    text = result.content[0].text if result.content else ""
    print(f"\n=== {title} {'(isError)' if result.is_error else ''}")
    try:
        data = json.loads(text)
        print(json.dumps(data, indent=2)[:2500])
        return data
    except json.JSONDecodeError:
        print(text)
        return {}


async def main(offline: bool, service: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    if offline:
        env["PD_MCP_FAKE"] = "1"
    elif not env.get("PAGERDUTY_API_TOKEN"):
        sys.exit("Set PAGERDUTY_API_TOKEN, or run with --offline.")

    params = StdioServerParameters(command=sys.executable, args=["-m", "pd_mcp.server"], env=env, cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("Tools:", ", ".join(t.name for t in tools.tools))

            # Q1: "Who's on call for the Payments service right now?"
            show(f'Q1 who_is_on_call(service="{service}")',
                 await session.call_tool("who_is_on_call", {"service": service}))

            # Q2: "Are there any open high-urgency incidents, and has anyone acknowledged them?"
            data = show('Q2 list_open_incidents(urgency="high")',
                        await session.call_tool("list_open_incidents", {"urgency": "high"}))

            # Follow-up: dig into the first one, the way an agent would
            if data.get("incidents"):
                first = data["incidents"][0]["id"]
                show(f'Q3 get_incident_context(incident="{first}")',
                     await session.call_tool("get_incident_context", {"incident": first}))

            # Edge cases the model has to handle
            show('Edge: ambiguous name who_is_on_call(service="pay")',
                 await session.call_tool("who_is_on_call", {"service": "pay"}))
            show('Edge: unknown incident', await session.call_tool("get_incident_context", {"incident": "QNOPE"}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--service", default="Payments")
    a = ap.parse_args()
    asyncio.run(main(a.offline, a.service))
