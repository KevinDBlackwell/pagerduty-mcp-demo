"""MCP wiring. Each tool is a thin wrapper: call the logic in tools.py, turn API failures into
ToolError so the client sees isError=true with a message the model can act on.

Run:  PAGERDUTY_API_TOKEN=... python -m pd_mcp.server
"""

from __future__ import annotations

import functools
import logging
import os
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import tools
from .client import PagerDutyClient, PagerDutyError

INSTRUCTIONS = """Read-only tools for a PagerDuty account's on-call coverage and open incidents.
Typical flows:
- "Who's on call for X?" -> who_is_on_call(service="X"). If it returns status "ambiguous", ask the user which candidate they mean.
- "Any open high-urgency incidents? Has anyone acknowledged them?" -> list_open_incidents(urgency="high"). Use summary counts and each incident's acknowledged / acknowledged_by fields.
- "What's going on with incident N?" -> get_incident_context(incident="N").
Times are UTC. Never invent people or contact details that a tool did not return."""

logging.getLogger("httpx").setLevel(logging.WARNING)  # keep request logs out of the MCP stderr stream

mcp = MCPServer("pagerduty-oncall", instructions=INSTRUCTIONS)
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

_client: PagerDutyClient | None = None


def client() -> PagerDutyClient:
    global _client
    if _client is None:
        if os.environ.get("PD_MCP_FAKE") == "1":
            # Offline demo mode: answer from the in-memory fake instead of the real API.
            from .fake_api import FakePagerDuty
            _client = PagerDutyClient("test-token", transport=FakePagerDuty().transport())
        else:
            _client = PagerDutyClient()
    return _client


def as_tool_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except PagerDutyError as e:
            raise ToolError(f"{e.message} {e.hint} (retryable: {'yes' if e.retryable else 'no'})") from e
    return wrapper


@mcp.tool(annotations=READ_ONLY)
@as_tool_errors
def who_is_on_call(
    service: Annotated[str, Field(description="Service name as the user said it (e.g. 'Payments'), or a PagerDuty service ID like 'PABC123'.")],
) -> dict:
    """Who is on call for a service right now, by escalation level, with first name, last name,
    and contact methods (email, phone, SMS). Level 1 is paged first.
    Returns status 'ambiguous' with candidates if the name matches several services."""
    return tools.who_is_on_call(client(), service)


@mcp.tool(annotations=READ_ONLY)
@as_tool_errors
def list_open_incidents(
    service: Annotated[str | None, Field(description="Optional service name or ID. Omit for all services.")] = None,
    urgency: Annotated[Literal["high", "low"] | None, Field(description="Optional: only high or only low urgency.")] = None,
    status: Annotated[Literal["triggered", "acknowledged"] | None, Field(description="Optional: 'triggered' means nobody has acknowledged it yet.")] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Max incidents to list. Summary counts always cover all matches.")] = 20,
) -> dict:
    """Open (triggered or acknowledged) incidents, high urgency and unacknowledged first.
    Includes summary counts and, per incident, whether it is acknowledged and by whom."""
    return tools.list_open_incidents(client(), service, urgency, status, limit)


@mcp.tool(annotations=READ_ONLY)
@as_tool_errors
def get_incident_context(
    incident: Annotated[str, Field(description="Incident ID (e.g. 'Q1ABC2DEF3GH') or incident number (e.g. '1234').")],
) -> dict:
    """Everything needed to start investigating one incident: status, urgency, service, who is
    assigned and who acknowledged, the first alerts with their details and links, runbook links
    found in alerts, notes, a short timeline, and the current responders."""
    return tools.get_incident_context(client(), incident)


@mcp.tool(annotations=READ_ONLY)
@as_tool_errors
def find_services(
    query: Annotated[str, Field(description="Part of a service name.")],
) -> dict:
    """Search services by name. Use when a service name is ambiguous or not found."""
    return tools.find_services(client(), query)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
