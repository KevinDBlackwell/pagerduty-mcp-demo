# PagerDuty on-call and incident tools for MCP

An MCP server that lets an LLM answer questions about a PagerDuty account's on-call coverage and open incidents:

- "Who's on call for the Payments service right now?"
- "Are there any open high-urgency incidents, and has anyone acknowledged them?"
- "What's going on with incident 1234?"

All tools are read-only.

## Quick start

Requires Python 3.10+.

macOS / Linux:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PAGERDUTY_API_TOKEN=<token>

python -m pytest -q                    # 28 tests, no network needed
python scripts/demo.py --offline       # scripted tool calls over MCP against an in-memory fake
python scripts/demo.py                 # the same calls against the real API
```

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1             # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
$env:PAGERDUTY_API_TOKEN = "<token>"

python -m pytest -q
python scripts\demo.py --offline
python scripts\demo.py
```

In cmd.exe, activate with `.venv\Scripts\activate.bat` and set the token with `set PAGERDUTY_API_TOKEN=<token>`.

Against the real API, the demo lists the account's services and asks about the first one that has someone on call. Pass `--service "<name>"` to pick a different one.

## Connect it to an LLM

**Claude Code**

```bash
claude mcp add pagerduty -e PAGERDUTY_API_TOKEN=$PAGERDUTY_API_TOKEN -- python -m pd_mcp.server
```

Run that from the repo root (or set `PYTHONPATH` to it), then ask Claude Code a question. In PowerShell, use `$env:PAGERDUTY_API_TOKEN` in place of `$PAGERDUTY_API_TOKEN`.

**Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "pagerduty": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "pd_mcp.server"],
      "cwd": "/absolute/path/to/pagerduty-oncall-mcp",
      "env": { "PAGERDUTY_API_TOKEN": "<token>" }
    }
  }
}
```

On Windows the command is `C:\\path\\to\\.venv\\Scripts\\python.exe` (backslashes doubled in JSON), and the config file is in `%APPDATA%\Claude\`.

**MCP Inspector:** `npx @modelcontextprotocol/inspector -e PAGERDUTY_API_TOKEN=<token> python -m pd_mcp.server`

**Scripted agent loop:** `scripts/agent.py` starts the server over stdio, hands Claude the tool list, and runs the tool-use loop.

```bash
pip install anthropic && export ANTHROPIC_API_KEY=...
python scripts/agent.py "Any open high-urgency incidents? Has anyone acknowledged them?"
python scripts/agent.py --offline "Who's on call for Payments?"   # fake PagerDuty, real model
```

In PowerShell, set the key with `$env:ANTHROPIC_API_KEY = "..."`.

## Tools

| Tool | Input | Returns |
| --- | --- | --- |
| `who_is_on_call` | service name or ID | On-call people by escalation level, with first name, last name, email, phone and SMS, how they're on call (schedule or direct), and until when |
| `list_open_incidents` | optional service, urgency, status, limit | Summary counts (open, high urgency, unacknowledged) plus incidents sorted for triage, each with `acknowledged` and `acknowledged_by` |
| `get_incident_context` | incident ID or number | Incident details, first alerts with details and links, runbook links found in alerts, notes, recent timeline, current responders |
| `find_services` | name fragment | Matching services, for when a name is ambiguous or not found |

## Design decisions

### Inputs and outputs are designed for an LLM, not a person

- **Accept what the user said.** Tools take a service *name* ("payments"), not just an ID. An exact case-insensitive match wins; a single partial match is used; several matches come back as `status: "ambiguous"` with candidates and a `next_step` telling the model to ask the user. This saves the model a lookup call and stops it guessing.
- **Expected states are results, not errors.** Ambiguous, not found and nobody on call are normal answers with a `next_step`. Only real failures (bad token, rate limit after retries, PagerDuty down) are tool errors.
- **Pre-compute what the model would otherwise infer.** `acknowledged`, `acknowledged_by`, `open_for_minutes`, summary counts, and a `role` per escalation level ("primary: paged first"). Models are bad at date math and at counting long lists; the tool does both.
- **Small, flat output.** Only the fields that answer the question. Long alert fields are clipped with a "[N more chars]" marker; lists are capped and say so (`showing: "5 of 33"`, `summary_is_partial`). The model can't mistake a partial list for the whole picture.
- **Descriptions and server instructions tell the model when to use each tool**, and enum arguments (`urgency`, `status`) are typed so invalid values are rejected by the schema before the call.
- **Read-only annotations** (`read_only_hint`, `idempotent_hint`) on every tool so clients can auto-approve them.

### Pagination, errors and rate limits

All in `pd_mcp/client.py`, so every tool gets the same behavior:

- **Pagination:** classic offset/limit, 100 per page, followed until `more` is false. A hard cap per call (500 by default) bounds latency and token cost on big accounts; hitting it sets `truncated`.
- **Rate limits:** on 429, wait for the `ratelimit-reset` header (or `Retry-After`), else exponential backoff with jitter; at most 3 retries, and never wait more than 30 seconds on one try.
- **Server errors and timeouts:** same retry policy.
- **Errors for the model:** each failure has a message and a hint (`"Do not retry"`, `"Wait about a minute"`), plus whether it's retryable. 401/403 are never retried.

### Assumptions

- **First and last name:** PagerDuty users have one `name` field. I split on the first space ("Ana de la Cruz" becomes "Ana" and "de la Cruz"). Single names return an empty last name. Real names don't split cleanly everywhere, so the user ID is returned too.
- **On call for a service** means on call for the service's escalation policy, at every level, right now. Level 1 is listed first. A person on call through two schedules at the same level is listed once.
- **Contact methods** come from `/users/{id}?include[]=contact_methods`, one call per on-call user, cached within the tool call. Push-notification methods are skipped because an agent can't use them.
- **Open** means `triggered` or `acknowledged`. `date_range=all` is passed because the incidents endpoint otherwise only searches a recent window.
- **Incident numbers:** `get_incident_context` accepts an ID or a number (`1234` or `#1234`) and passes it to `GET /incidents/{id}`.
- **Times are UTC.**

### Tradeoffs

- **Synchronous httpx.** Simple to read and debug. The N+1 contact lookups are sequential; with a handful of on-call users that's fine. At larger scale I'd make the client async and fetch users concurrently with a semaphore.
- **Sorting client-side** for triage order (high urgency, then unacknowledged, then newest) instead of relying on the API's `sort_by`, so the order is explicit and tested.
- **No caching across calls.** On-call data changes at shift boundaries and incidents change by the second; stale answers here are worse than slow ones.
- **A fake PagerDuty** (`pd_mcp/fake_api.py`) as an httpx transport instead of recorded cassettes. The tests and the offline demo use the same fake, and it caps pages at 25 so pagination is exercised with small data.

## Testing

`python -m pytest -q` runs 28 tests with no network:

- Unit tests for name splitting and contact-method normalization.
- Tool tests against the fake API: name resolution (exact, ambiguous, not found, by ID), on-call levels and de-duplication, contact lookups cached, nobody on call, pagination across pages, triage order, filters, clipping, not-found incidents.
- Client policy tests: 429 retried using the reset header, 5xx retried then a retryable error, 401 not retried, truncation at the cap.
- MCP-level tests: tools are listed read-only with descriptions, calls return JSON, schema rejects bad enums, API failures and a missing token surface as tool errors with hints.

## Before letting an agent use this on a real customer account

- **Auth per customer:** OAuth or scoped tokens per tenant instead of one shared env-var token; never log tokens.
- **Least privilege:** a read-only token for these tools. Any write tool (acknowledge, add note, reassign) would be a separate tool with `destructive_hint`, explicit confirmation, and an audit trail.
- **PII:** phone numbers and emails go into model context. Decide with the customer whether to return them by default, mask them, or only return them when the user asks to contact someone.
- **Rate limits across many users:** a shared per-account budget (token bucket) instead of per-process retries, so one chatty agent can't starve the customer's own integrations.
- **Observability:** structured logs per tool call (tool, account, latency, API calls made, truncation, errors), without PII.
- **Prompt injection:** alert details and notes are written by other systems and people. Treat them as untrusted data, keep them clearly labeled in output, and never let them drive write actions without a human.
- **Evals:** a set of real questions with expected answers, run against a sandbox account on every change to tool descriptions or output shape.

## Notes on the starter code

- The starter file is named `mcp.py`, which shadows the `mcp` package, so `from mcp.server...` fails when run from that folder. This repo uses a `pd_mcp` package instead.
- The starter's docstring refers to `server.py`; here the entry point is `python -m pd_mcp.server`.

## How I used AI

I used Claude (Anthropic) as a pair programmer for this assignment.

**What Claude did:** drafted the PagerDuty client, the four tools, the in-memory fake API, the tests, and the two demo scripts, based on the assignment and PagerDuty's API reference.

**What I did:**

- Set the requirements: read-only tools, inputs a person would actually type (service names, not IDs), and output shaped for a model to read rather than a person.
- Reviewed the design decisions and kept the ones I agree with and can defend: ambiguous and not-found results returned as data with a next step instead of errors, pre-computed fields like `acknowledged` and `open_for_minutes`, capped pagination that says when it truncated, and one client that owns retries and rate limits.
- Ran the tests and both demos myself on Windows against PagerDuty's example account.
- Fixed what that run exposed. The first live run returned "not found" because the demo was hard-coded to a service name from the fake data, so the demo now lists the account's services and uses a real one. Setup steps also assumed macOS or Linux, so I added Windows instructions.

**What I checked by hand:** that the real API matches what the fake assumes, especially current on-calls from `/oncalls`, contact methods from `/users/{id}?include[]=contact_methods`, and looking up an incident by its number.

## Layout

```
pd_mcp/
  client.py     HTTP, pagination, retries, errors
  tools.py      tool logic (pure functions over the client)
  server.py     MCP wiring and tool descriptions
  fake_api.py   in-memory PagerDuty for tests and offline demo
scripts/
  demo.py       scripted tool-call sequence over MCP stdio
  agent.py      real Claude tool-use loop over MCP stdio
tests/
```
