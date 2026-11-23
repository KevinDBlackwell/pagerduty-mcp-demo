"""Tool logic, separate from the MCP layer so it can be tested and called directly.

- Services can be given by name, partial name, or ID.
- Output is small and flat. Derived fields (acknowledged, open_for_minutes, level role)
  are computed here instead of left to the model.
- "ambiguous" and "not_found" are returned as data with a next_step. Only API failures raise.
- Capped lists say so (`truncated`, `summary_is_partial`).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .client import PagerDutyClient, PagerDutyError

PD_ID = re.compile(r"^P[A-Z0-9]{6}$")
OPEN_STATUSES = ("triggered", "acknowledged")
URGENCIES = ("high", "low")
TEXT_LIMIT = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _minutes_since(ts: str | None, now: datetime) -> int | None:
    t = _parse(ts)
    return None if t is None else max(0, int((now - t).total_seconds() // 60))


def _clip(value: Any, limit: int = TEXT_LIMIT) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"... [{len(value) - limit} more chars]"
    return value


def split_name(full: str) -> tuple[str, str]:
    """PagerDuty stores one `name` field. Split on the first space: 'Ana de la Cruz' -> ('Ana', 'de la Cruz')."""
    parts = (full or "").strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""


def normalize_contact_methods(methods: list[dict]) -> dict[str, list[dict]]:
    """Group PagerDuty contact methods into email / phone / sms, with phone numbers in + format."""
    out: dict[str, list[dict]] = {"email": [], "phone": [], "sms": []}
    for m in methods:
        kind = m.get("type", "")
        label = m.get("label") or ""
        if kind.startswith("email_contact_method"):
            out["email"].append({"address": m.get("address"), "label": label})
        elif kind.startswith("phone_contact_method") or kind.startswith("sms_contact_method"):
            cc = m.get("country_code")
            number = f"+{cc} {m.get('address')}" if cc else m.get("address")
            key = "phone" if kind.startswith("phone") else "sms"
            out[key].append({"number": number, "label": label})
        # push notifications are skipped, an agent can't use them
    return out


def find_services(client: PagerDutyClient, query: str, max_results: int = 10) -> dict:
    services, truncated = client.paginate("/services", "services", {"query": query}, max_items=100)
    rows = [{"id": s["id"], "name": s["name"]} for s in services]
    return {
        "query": query,
        "count": len(rows),
        "services": rows[:max_results],
        "truncated": truncated or len(rows) > max_results,
    }


def resolve_service(client: PagerDutyClient, service: str) -> dict:
    """Turn a name or ID into one service.

    Returns {"status": "ok", "service": {...}} or {"status": "ambiguous" | "not_found", ...}.
    """
    service = (service or "").strip()
    if not service:
        return {"status": "not_found", "message": "No service was given.",
                "next_step": "Ask the user which service they mean."}

    if PD_ID.match(service):
        try:
            return {"status": "ok", "service": client.get(f"/services/{service}")["service"]}
        except PagerDutyError as e:
            if e.status != 404:
                raise
            return {"status": "not_found", "message": f"No service has the ID {service}.",
                    "next_step": "Call find_services with the service name to get its ID."}

    matches, _ = client.paginate("/services", "services", {"query": service}, max_items=100)
    exact = [s for s in matches if s["name"].lower() == service.lower()]
    if len(exact) == 1:
        return {"status": "ok", "service": exact[0]}
    if len(matches) == 1:
        return {"status": "ok", "service": matches[0]}
    if not matches:
        return {"status": "not_found",
                "message": f"No PagerDuty service matches '{service}'.",
                "next_step": "Call find_services with a shorter or different name, or ask the user."}
    candidates = exact or matches
    return {"status": "ambiguous",
            "message": f"{len(candidates)} services match '{service}'.",
            "candidates": [{"id": s["id"], "name": s["name"]} for s in candidates[:10]],
            "next_step": "Ask the user which one they mean, then call again with its id."}


def _user_with_contacts(client: PagerDutyClient, user_id: str, cache: dict) -> dict:
    if user_id not in cache:
        data = client.get(f"/users/{user_id}", {"include[]": "contact_methods"})
        cache[user_id] = data["user"]
    return cache[user_id]


def _oncall_levels(client: PagerDutyClient, ep_id: str, now: datetime,
                   with_contacts: bool = True, max_level: int | None = None) -> list[dict]:
    oncalls, _ = client.paginate("/oncalls", "oncalls",
                                 {"escalation_policy_ids[]": [ep_id], "time_zone": "UTC"},
                                 max_items=200)
    cache: dict[str, dict] = {}
    levels: dict[int, dict[str, dict]] = {}
    for oc in oncalls:
        level = oc.get("escalation_level") or 1
        if max_level and level > max_level:
            continue
        user_ref = oc.get("user") or {}
        uid = user_ref.get("id")
        if not uid or uid in levels.get(level, {}):
            continue  # same person on two schedules at one level
        if with_contacts:
            user = _user_with_contacts(client, uid, cache)
            name = user.get("name") or user_ref.get("summary", "")
        else:
            user, name = {}, user_ref.get("summary", "")
        first, last = split_name(name)
        entry = {
            "user_id": uid,
            "first_name": first,
            "last_name": last,
            "on_call_via": (oc.get("schedule") or {}).get("summary") or "assigned directly on the escalation policy",
            "on_call_until": oc.get("end") or "indefinitely (no end time)",
        }
        if with_contacts:
            entry["login_email"] = user.get("email")
            entry["contact_methods"] = normalize_contact_methods(user.get("contact_methods", []))
        levels.setdefault(level, {})[uid] = entry
    return [
        {"level": lvl,
         "role": "primary: paged first" if lvl == 1 else f"escalation level {lvl}: paged if level {lvl - 1} doesn't respond",
         "people": list(people.values())}
        for lvl, people in sorted(levels.items())
    ]


def who_is_on_call(client: PagerDutyClient, service: str) -> dict:
    resolved = resolve_service(client, service)
    if resolved["status"] != "ok":
        return resolved
    svc = resolved["service"]
    ep = svc.get("escalation_policy") or {}
    now = _now()
    levels = _oncall_levels(client, ep["id"], now) if ep.get("id") else []
    result = {
        "status": "ok",
        "as_of": now.isoformat(timespec="seconds"),
        "service": {"id": svc["id"], "name": svc["name"]},
        "escalation_policy": {"id": ep.get("id"), "name": ep.get("summary")},
        "on_call": levels,
    }
    if not levels:
        result["note"] = ("Nobody is on call for this service right now. "
                          "Its escalation policy has no active on-call users.")
    return result


def _incident_row(inc: dict, now: datetime) -> dict:
    acks = inc.get("acknowledgements") or []
    return {
        "id": inc["id"],
        "number": inc.get("incident_number"),
        "title": _clip(inc.get("title"), 200),
        "status": inc.get("status"),
        "urgency": inc.get("urgency"),
        "priority": (inc.get("priority") or {}).get("summary"),
        "service": (inc.get("service") or {}).get("summary"),
        "created_at": inc.get("created_at"),
        "open_for_minutes": _minutes_since(inc.get("created_at"), now),
        "acknowledged": inc.get("status") == "acknowledged",
        "acknowledged_by": sorted({(a.get("acknowledger") or {}).get("summary") for a in acks} - {None}),
        "assigned_to": [(a.get("assignee") or {}).get("summary") for a in inc.get("assignments") or []],
        "url": inc.get("html_url"),
    }


def list_open_incidents(client: PagerDutyClient, service: str | None = None,
                        urgency: str | None = None, status: str | None = None,
                        limit: int = 20) -> dict:
    if urgency and urgency not in URGENCIES:
        return {"status": "invalid_argument", "message": f"urgency must be one of {list(URGENCIES)}."}
    if status and status not in OPEN_STATUSES:
        return {"status": "invalid_argument",
                "message": f"status must be one of {list(OPEN_STATUSES)}. Resolved incidents are not open."}
    limit = max(1, min(limit, 100))

    params: dict[str, Any] = {
        "statuses[]": [status] if status else list(OPEN_STATUSES),
        "date_range": "all",  # otherwise the API only searches a recent window
        "time_zone": "UTC",
    }
    if urgency:
        params["urgencies[]"] = [urgency]
    svc = None
    if service:
        resolved = resolve_service(client, service)
        if resolved["status"] != "ok":
            return resolved
        svc = resolved["service"]
        params["service_ids[]"] = [svc["id"]]

    incidents, truncated = client.paginate("/incidents", "incidents", params, max_items=500)
    now = _now()
    rows = [_incident_row(i, now) for i in incidents]
    # high urgency, then unacknowledged, then newest (sort is stable)
    rows.sort(key=lambda r: r["created_at"] or "", reverse=True)
    rows.sort(key=lambda r: (r["urgency"] != "high", r["acknowledged"]))

    summary = {
        "open": len(rows),
        "high_urgency": sum(r["urgency"] == "high" for r in rows),
        "unacknowledged": sum(not r["acknowledged"] for r in rows),
        "high_urgency_unacknowledged": sum(r["urgency"] == "high" and not r["acknowledged"] for r in rows),
    }
    return {
        "status": "ok",
        "as_of": now.isoformat(timespec="seconds"),
        "filters": {"service": svc["name"] if svc else "all services", "urgency": urgency or "any",
                    "status": status or "triggered or acknowledged"},
        "summary": summary,
        "summary_is_partial": truncated,
        "incidents": rows[:limit],
        "showing": f"{min(limit, len(rows))} of {len(rows)}" + (" (more exist beyond the 500 fetched)" if truncated else ""),
    }


def _alert_row(alert: dict) -> dict:
    body = alert.get("body") or {}
    cef = body.get("cef_details") or {}
    details = body.get("details") or cef.get("details") or {}
    if isinstance(details, dict):
        details = {k: _clip(v, 300) for k, v in list(details.items())[:15]}
    else:
        details = _clip(details, 800)
    links = [c.get("href") for c in (cef.get("contexts") or body.get("contexts") or []) if c.get("href")]
    return {
        "id": alert["id"],
        "summary": _clip(alert.get("summary"), 300),
        "severity": alert.get("severity"),
        "status": alert.get("status"),
        "created_at": alert.get("created_at"),
        "source": cef.get("source_origin") or cef.get("source_component"),
        "details": details,
        "links": links[:5],
    }


def _find_runbooks(alerts: list[dict]) -> list[str]:
    found: list[str] = []
    for a in alerts:
        details = a.get("details") if isinstance(a.get("details"), dict) else {}
        for k, v in details.items():
            if "runbook" in k.lower():
                if isinstance(v, dict):
                    found.extend(str(x) for x in v.values())
                elif v:
                    found.append(str(v))
        found.extend(l for l in a.get("links", []) if "runbook" in l.lower())
    return list(dict.fromkeys(found))[:5]


def get_incident_context(client: PagerDutyClient, incident: str,
                         max_alerts: int = 5, max_timeline: int = 15) -> dict:
    ref = (incident or "").strip().lstrip("#")
    if not ref:
        return {"status": "not_found", "message": "No incident was given.",
                "next_step": "Call list_open_incidents to find the incident ID."}

    try:
        inc = client.get(f"/incidents/{ref}")["incident"]
    except PagerDutyError as e:
        if e.status != 404:
            raise
        return {"status": "not_found", "message": f"No incident matches '{incident}'.",
                "next_step": "Call list_open_incidents to find the incident's ID or number."}
    iid = inc["id"]
    now = _now()

    alerts, alerts_trunc = client.paginate(f"/incidents/{iid}/alerts", "alerts", {}, max_items=50)
    notes, _ = client.paginate(f"/incidents/{iid}/notes", "notes", {}, max_items=20)
    log, log_trunc = client.paginate(f"/incidents/{iid}/log_entries", "log_entries",
                                     {"is_overview": "true", "time_zone": "UTC"}, max_items=100)

    alert_rows = [_alert_row(a) for a in sorted(alerts, key=lambda a: a.get("created_at") or "")]
    timeline = sorted(
        ({"at": e.get("created_at"), "event": e.get("type", "").replace("_log_entry", ""),
          "summary": _clip(e.get("summary"), 200)} for e in log),
        key=lambda e: e["at"] or "")
    ep = inc.get("escalation_policy") or {}
    responders = _oncall_levels(client, ep["id"], now, with_contacts=False, max_level=2) if ep.get("id") else []

    row = _incident_row(inc, now)
    return {
        "status": "ok",
        "as_of": now.isoformat(timespec="seconds"),
        "incident": {
            **row,
            "description": _clip(inc.get("description") or inc.get("summary")),
            "escalation_policy": ep.get("summary"),
            "last_status_change_at": inc.get("last_status_change_at"),
            "minutes_since_last_change": _minutes_since(inc.get("last_status_change_at"), now),
        },
        "alerts": {
            "total": len(alerts) if not alerts_trunc else f"{len(alerts)}+",
            "first_seen": alert_rows[0]["created_at"] if alert_rows else None,
            "shown": alert_rows[:max_alerts],
        },
        "runbooks": _find_runbooks(alert_rows),
        "notes": [{"at": n.get("created_at"), "author": (n.get("user") or {}).get("summary"),
                   "text": _clip(n.get("content"))} for n in notes],
        "timeline": timeline[-max_timeline:],
        "timeline_note": f"Last {min(max_timeline, len(timeline))} of {len(timeline)}{'+' if log_trunc else ''} events.",
        "current_responders": responders,
        "next_step_hint": ("Use who_is_on_call with the service name for full contact details. "
                           "Use the incident url to open it in PagerDuty."),
    }
