"""End-to-end through the MCP layer: list tools, call them, check errors surface as isError."""

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pd_mcp import server
from pd_mcp.client import PagerDutyClient
from pd_mcp.fake_api import FakePagerDuty


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    fake = FakePagerDuty()
    monkeypatch.setattr(server, "_client", PagerDutyClient("test-token", transport=fake.transport(), sleep=lambda s: None))
    return fake


def run(coro):
    return asyncio.run(coro)


def test_tools_are_listed_read_only_with_descriptions():
    listed = {t.name: t for t in run(server.mcp.list_tools())}
    assert {"who_is_on_call", "list_open_incidents", "get_incident_context", "find_services"} <= set(listed)
    for t in listed.values():
        assert t.annotations.read_only_hint is True
        assert len(t.description) > 40


def test_call_tool_returns_structured_content():
    result = run(server.mcp.call_tool("who_is_on_call", {"service": "Payments"}))
    assert result.is_error is False
    data = json.loads(result.content[0].text)  # dict results are returned as JSON text
    assert data["on_call"][0]["people"][0]["last_name"] == "de la Cruz"


def test_invalid_enum_is_rejected_by_schema():
    with pytest.raises(ToolError):
        run(server.mcp.call_tool("list_open_incidents", {"urgency": "urgent"}))


def test_api_failure_becomes_tool_error_with_hint(fake_client):
    fake_client.fail_next = [(401, {})]
    with pytest.raises(ToolError) as e:
        run(server.mcp.call_tool("find_services", {"query": "x"}))
    assert "token" in str(e.value) and "Do not retry" in str(e.value)
