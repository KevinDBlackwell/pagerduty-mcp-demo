"""An in-memory fake of the PagerDuty endpoints these tools use.

Used by the tests and by `scripts/demo.py --offline`, so the demo runs with no network.
Response shapes follow the PagerDuty REST API v2 reference. Page size is capped at 25 here
(the real API allows 100) so pagination is exercised with a small dataset.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def ts(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def ref(kind: str, id_: str, summary: str) -> dict:
    return {"id": id_, "type": f"{kind}_reference", "summary": summary}


USERS = {
    "PUSER01": {"id": "PUSER01", "name": "Ana de la Cruz", "email": "ana@northwind.example", "contact_methods": [
        {"type": "email_contact_method", "address": "ana@northwind.example", "label": "Work"},
        {"type": "phone_contact_method", "address": "2015550101", "country_code": 1, "label": "Mobile"},
        {"type": "sms_contact_method", "address": "2015550101", "country_code": 1, "label": "Mobile"},
        {"type": "push_notification_contact_method", "address": "iPhone", "label": "iPhone"}]},
    "PUSER02": {"id": "PUSER02", "name": "Ben Okafor", "email": "ben@northwind.example", "contact_methods": [
        {"type": "email_contact_method", "address": "ben@northwind.example", "label": "Work"},
        {"type": "phone_contact_method", "address": "7325550199", "country_code": 1, "label": "Mobile"}]},
    "PUSER03": {"id": "PUSER03", "name": "Chen", "email": "chen@northwind.example", "contact_methods": [
        {"type": "email_contact_method", "address": "chen@northwind.example", "label": "Work"}]},
}

EPS = {"PEP0001": "Payments EP", "PEP0002": "Payments Batch EP", "PEP0003": "Checkout EP", "PEP0004": "Search EP"}
SERVICES = [
    {"id": "PSVC001", "name": "Payments", "escalation_policy": ref("escalation_policy", "PEP0001", "Payments EP")},
    {"id": "PSVC002", "name": "Payments Batch Jobs", "escalation_policy": ref("escalation_policy", "PEP0002", "Payments Batch EP")},
    {"id": "PSVC003", "name": "Checkout", "escalation_policy": ref("escalation_policy", "PEP0003", "Checkout EP")},
    {"id": "PSVC004", "name": "Search", "escalation_policy": ref("escalation_policy", "PEP0004", "Search EP")},
]

ONCALLS = [
    # Ana is on level 1 through two schedules: the tool should list her once.
    {"escalation_policy": ref("escalation_policy", "PEP0001", "Payments EP"), "escalation_level": 1,
     "schedule": ref("schedule", "PSCH001", "Payments Primary"), "user": ref("user", "PUSER01", "Ana de la Cruz"),
     "start": ts(600), "end": ts(-840)},
    {"escalation_policy": ref("escalation_policy", "PEP0001", "Payments EP"), "escalation_level": 1,
     "schedule": ref("schedule", "PSCH002", "Payments Follow-the-sun"), "user": ref("user", "PUSER01", "Ana de la Cruz"),
     "start": ts(600), "end": ts(-840)},
    {"escalation_policy": ref("escalation_policy", "PEP0001", "Payments EP"), "escalation_level": 2,
     "schedule": None, "user": ref("user", "PUSER02", "Ben Okafor"), "start": None, "end": None},
    {"escalation_policy": ref("escalation_policy", "PEP0002", "Payments Batch EP"), "escalation_level": 1,
     "schedule": ref("schedule", "PSCH003", "Batch On-Call"), "user": ref("user", "PUSER03", "Chen"),
     "start": ts(60), "end": ts(-1380)},
    # Checkout EP has nobody on call (gap in coverage).
]


def _incident(n: int, svc: dict, status: str, urgency: str, minutes_ago: int, title: str) -> dict:
    inc = {
        "id": f"Q{n:011d}", "type": "incident", "incident_number": 1000 + n, "title": title,
        "description": title, "status": status, "urgency": urgency, "created_at": ts(minutes_ago),
        "last_status_change_at": ts(max(minutes_ago - 5, 0)),
        "service": ref("service", svc["id"], svc["name"]),
        "escalation_policy": svc["escalation_policy"],
        "priority": {"summary": "P1"} if urgency == "high" else None,
        "assignments": [{"at": ts(minutes_ago), "assignee": ref("user", "PUSER01", "Ana de la Cruz")}],
        "acknowledgements": [], "html_url": f"https://northwind.pagerduty.com/incidents/Q{n:011d}",
    }
    if status == "acknowledged":
        inc["acknowledgements"] = [{"at": ts(minutes_ago - 3), "acknowledger": ref("user", "PUSER01", "Ana de la Cruz")}]
    return inc


INCIDENTS = [
    _incident(1, SERVICES[0], "triggered", "high", 12, "Payments API 5xx rate above 5%"),
    _incident(2, SERVICES[0], "acknowledged", "high", 45, "Card authorization latency p99 over 2s"),
    _incident(3, SERVICES[1], "triggered", "low", 300, "Nightly settlement job late"),
    _incident(4, SERVICES[0], "resolved", "high", 2000, "Old resolved incident"),
] + [
    # bulk low-urgency noise on Search so pagination kicks in
    _incident(10 + i, SERVICES[3], "acknowledged", "low", 100 + i, f"Search index lag warning #{i}") for i in range(30)
]

ALERTS = {
    "Q00000000001": [
        {"id": "PALRT01", "summary": "5xx rate 8.2% on payments-api", "severity": "critical", "status": "triggered",
         "created_at": ts(12),
         "body": {"cef_details": {"source_origin": "datadog", "details": {
             "error_rate_pct": 8.2, "threshold_pct": 5, "region": "us-east-1",
             "runbook_url": {"github": "https://github.com/northwind/runbooks/blob/main/payments-5xx.md"},
             "log_query": "service:payments-api status:error"},
             "contexts": [{"type": "link", "href": "https://app.datadoghq.com/monitors/48213", "text": "Monitor"}]}}},
        {"id": "PALRT02", "summary": "Pod restarts on payments-api", "severity": "warning", "status": "triggered",
         "created_at": ts(10), "body": {"details": {"restarts": 4, "namespace": "payments", "trace": "x" * 2000}}},
    ],
}
NOTES = {"Q00000000001": [{"id": "PNOTE1", "content": "Deploy 4f2a went out at 14:02. Suspect it.",
                           "created_at": ts(8), "user": ref("user", "PUSER02", "Ben Okafor")}]}
LOGS = {"Q00000000001": [
    {"type": "trigger_log_entry", "created_at": ts(12), "summary": "Triggered through the API."},
    {"type": "notify_log_entry", "created_at": ts(12), "summary": "Notified Ana de la Cruz by phone."},
    {"type": "annotate_log_entry", "created_at": ts(8), "summary": "Note added by Ben Okafor."},
]}


class FakePagerDuty:
    """httpx transport that answers like PagerDuty. Set `fail_next` to inject errors."""

    PAGE_MAX = 25

    def __init__(self):
        self.calls: list[str] = []
        self.fail_next: list[tuple[int, dict]] = []  # (status, headers) consumed one per request

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}?{request.url.query.decode()}")
        if self.fail_next:
            status, headers = self.fail_next.pop(0)
            return httpx.Response(status, headers=headers, json={"error": {"message": "injected"}})
        if request.headers.get("Authorization") != "Token token=test-token":
            return httpx.Response(401, json={"error": {"message": "Unauthorized"}})
        return self.route(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    # -- routing --
    def route(self, req: httpx.Request) -> httpx.Response:
        p, q = req.url.path, req.url.params
        parts = p.strip("/").split("/")
        if p == "/services":
            query = (q.get("query") or "").lower()
            return self.page([s for s in SERVICES if query in s["name"].lower()], "services", q)
        if parts[0] == "services" and len(parts) == 2:
            s = next((s for s in SERVICES if s["id"] == parts[1]), None)
            return self.ok({"service": s}) if s else self.not_found()
        if p == "/oncalls":
            eps = q.get_list("escalation_policy_ids[]")
            return self.page([o for o in ONCALLS if not eps or o["escalation_policy"]["id"] in eps], "oncalls", q)
        if parts[0] == "users" and len(parts) == 2:
            u = USERS.get(parts[1])
            return self.ok({"user": u}) if u else self.not_found()
        if p == "/incidents":
            statuses, svcs, urg = q.get_list("statuses[]"), q.get_list("service_ids[]"), q.get_list("urgencies[]")
            rows = [i for i in INCIDENTS
                    if (not statuses or i["status"] in statuses)
                    and (not svcs or i["service"]["id"] in svcs)
                    and (not urg or i["urgency"] in urg)]
            return self.page(rows, "incidents", q)
        if parts[0] == "incidents" and len(parts) >= 2:
            key = parts[1]
            inc = next((i for i in INCIDENTS if i["id"] == key or str(i["incident_number"]) == key), None)
            if not inc:
                return self.not_found()
            if len(parts) == 2:
                return self.ok({"incident": inc})
            sub = parts[2]
            data = {"alerts": ALERTS, "notes": NOTES, "log_entries": LOGS}[sub].get(inc["id"], [])
            if sub == "notes":
                return self.ok({"notes": data})  # notes are not paginated in the real API
            return self.page(data, sub, q)
        return self.not_found()

    def page(self, rows, key, q):
        limit = min(int(q.get("limit", 25)), self.PAGE_MAX)
        offset = int(q.get("offset", 0))
        chunk = rows[offset:offset + limit]
        return self.ok({key: chunk, "limit": limit, "offset": offset, "total": None,
                        "more": offset + limit < len(rows)})

    @staticmethod
    def ok(body):
        return httpx.Response(200, content=json.dumps(body), headers={"content-type": "application/json"})

    @staticmethod
    def not_found():
        return httpx.Response(404, json={"error": {"message": "Not Found", "code": 2100}})
