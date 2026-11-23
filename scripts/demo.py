"""Runs a fixed set of tool calls over MCP stdio: the two example questions, a follow-up,
and a couple of edge cases.

    python scripts/demo.py --offline        # no network: in-memory fake PagerDuty
    PAGERDUTY_API_TOKEN=... python scripts/demo.py                    # first service with someone on call
    PAGERDUTY_API_TOKEN=... python scripts/demo.py --service "Widget Pro Beta"
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


def ambiguous_fragment(names: list[str]) -> str | None:
    """A short lowercase prefix of a first word that several service names share, e.g. 'pay'."""
    firsts = [n.split()[0] for n in names if n.split()]
    for word in firsts:
        if firsts.count(word) > 1:
            return word[:3].lower()
    return None


async def main(offline: bool, service: str | None) -> None:
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

            # use names from the account, not the fake data
            listed = json.loads((await session.call_tool("find_services", {"query": ""})).content[0].text)
            names = [s["name"] for s in listed.get("services", [])]
            more = ", ..." if listed.get("count", 0) > len(names) else ""
            print(f"Services ({listed.get('count', 0)}):", ", ".join(names) + more)
            if not names:
                sys.exit("The account has no services.")
            fragment = ambiguous_fragment(names)

            # Q1: "Who's on call for the <service> service right now?"
            # Without --service, use the first listed service that has someone on call.
            for candidate in [service] if service else names:
                result = await session.call_tool("who_is_on_call", {"service": candidate})
                data = json.loads(result.content[0].text) if not result.is_error else {}
                if service or data.get("on_call"):
                    break
                print(f"(nobody on call for {candidate}, trying the next service)")
            show(f'Q1 who_is_on_call(service="{candidate}")', result)

            # Q2: "Are there any open high-urgency incidents, and has anyone acknowledged them?"
            data = show('Q2 list_open_incidents(urgency="high")',
                        await session.call_tool("list_open_incidents", {"urgency": "high"}))

            # follow up on the first incident
            if data.get("incidents"):
                first = data["incidents"][0]["id"]
                show(f'Q3 get_incident_context(incident="{first}")',
                     await session.call_tool("get_incident_context", {"incident": first}))

            # edge cases
            if fragment:
                show(f'Edge: ambiguous name who_is_on_call(service="{fragment}")',
                     await session.call_tool("who_is_on_call", {"service": fragment}))
            show('Edge: unknown incident', await session.call_tool("get_incident_context", {"incident": "QNOPE"}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--service", help="service name for Q1 (default: the first service with someone on call)")
    a = ap.parse_args()
    asyncio.run(main(a.offline, a.service))
