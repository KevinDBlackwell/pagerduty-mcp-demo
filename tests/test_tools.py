import pytest

from pd_mcp import tools
from pd_mcp.client import PagerDutyClient, PagerDutyError
from pd_mcp.fake_api import FakePagerDuty


@pytest.fixture
def fake():
    return FakePagerDuty()


@pytest.fixture
def client(fake):
    sleeps = []
    c = PagerDutyClient("test-token", transport=fake.transport(), sleep=sleeps.append)
    c.sleeps = sleeps
    return c


# ---- helpers -------------------------------------------------------------------

def test_split_name_handles_multi_word_and_single_names():
    assert tools.split_name("Ana de la Cruz") == ("Ana", "de la Cruz")
    assert tools.split_name("Chen") == ("Chen", "")
    assert tools.split_name("") == ("", "")


def test_contact_methods_grouped_and_push_skipped():
    out = tools.normalize_contact_methods([
        {"type": "email_contact_method", "address": "a@x.io"},
        {"type": "phone_contact_method", "address": "2015550101", "country_code": 1},
        {"type": "sms_contact_method", "address": "2015550101", "country_code": 44},
        {"type": "push_notification_contact_method", "address": "iPhone"},
    ])
    assert out["email"][0]["address"] == "a@x.io"
    assert out["phone"][0]["number"] == "+1 2015550101"
    assert out["sms"][0]["number"] == "+44 2015550101"
    assert "push" not in str(out)


# ---- service resolution --------------------------------------------------------

def test_exact_name_wins_over_partial_matches(client):
    # "Payments" also matches "Payments Batch Jobs"; the exact name should win
    r = tools.resolve_service(client, "payments")
    assert r["status"] == "ok" and r["service"]["id"] == "PSVC001"


def test_ambiguous_name_returns_candidates_not_an_error(client):
    r = tools.resolve_service(client, "pay")
    assert r["status"] == "ambiguous"
    assert {c["name"] for c in r["candidates"]} == {"Payments", "Payments Batch Jobs"}
    assert "Ask the user" in r["next_step"]


def test_unknown_service_is_not_found_with_next_step(client):
    r = tools.resolve_service(client, "billing")
    assert r["status"] == "not_found" and "find_services" in r["next_step"]


def test_service_id_is_looked_up_directly(client, fake):
    r = tools.resolve_service(client, "PSVC004")
    assert r["service"]["name"] == "Search"
    assert fake.calls[-1].startswith("GET /services/PSVC004")


# ---- who is on call ------------------------------------------------------------

def test_on_call_returns_levels_names_and_contacts(client):
    r = tools.who_is_on_call(client, "Payments")
    assert r["status"] == "ok"
    level1, level2 = r["on_call"]
    assert level1["level"] == 1 and len(level1["people"]) == 1  # Ana via two schedules, listed once
    ana = level1["people"][0]
    assert (ana["first_name"], ana["last_name"]) == ("Ana", "de la Cruz")
    assert ana["contact_methods"]["phone"][0]["number"] == "+1 2015550101"
    assert ana["contact_methods"]["sms"], "SMS should be present"
    assert level2["people"][0]["on_call_via"].startswith("assigned directly")
    assert level2["people"][0]["on_call_until"].startswith("indefinitely")


def test_contact_lookups_are_cached_per_call(client, fake):
    tools.who_is_on_call(client, "Payments")
    user_calls = [c for c in fake.calls if c.startswith("GET /users/")]
    assert len(user_calls) == 2  # Ana once despite two schedules, Ben once


def test_nobody_on_call_is_explicit(client):
    r = tools.who_is_on_call(client, "Checkout")
    assert r["on_call"] == [] and "Nobody is on call" in r["note"]


def test_ambiguous_service_passes_through(client):
    assert tools.who_is_on_call(client, "pay")["status"] == "ambiguous"


# ---- open incidents ------------------------------------------------------------

def test_open_incidents_paginates_and_excludes_resolved(client, fake):
    r = tools.list_open_incidents(client, limit=100)
    assert r["summary"]["open"] == 33  # 3 payments/batch + 30 search, resolved excluded
    assert len([c for c in fake.calls if c.startswith("GET /incidents?")]) == 2  # 25 + 8
    assert all(i["status"] != "resolved" for i in r["incidents"])
    assert "date_range=all" in fake.calls[0]


def test_triage_order_and_ack_fields(client):
    r = tools.list_open_incidents(client, urgency="high")
    first, second = r["incidents"]
    assert first["number"] == 1001 and first["acknowledged"] is False
    assert second["acknowledged"] is True and second["acknowledged_by"] == ["Ana de la Cruz"]
    assert r["summary"]["high_urgency_unacknowledged"] == 1


def test_limit_trims_list_but_not_summary(client):
    r = tools.list_open_incidents(client, limit=5)
    assert len(r["incidents"]) == 5 and r["summary"]["open"] == 33
    assert r["showing"] == "5 of 33"


def test_service_filter_resolves_name(client, fake):
    r = tools.list_open_incidents(client, service="Payments")
    assert r["summary"]["open"] == 2 and "service_ids%5B%5D=PSVC001" in fake.calls[-1]


def test_bad_filter_values_are_explained(client):
    assert tools.list_open_incidents(client, status="resolved")["status"] == "invalid_argument"
    assert tools.list_open_incidents(client, urgency="urgent")["status"] == "invalid_argument"


# ---- incident context ----------------------------------------------------------

def test_incident_context_by_number(client):
    r = tools.get_incident_context(client, "#1001")
    assert r["incident"]["id"] == "Q00000000001"
    assert r["alerts"]["total"] == 2 and r["alerts"]["first_seen"]
    assert r["runbooks"] == ["https://github.com/northwind/runbooks/blob/main/payments-5xx.md"]
    assert r["notes"][0]["author"] == "Ben Okafor"
    assert [e["event"] for e in r["timeline"]] == ["trigger", "notify", "annotate"]
    assert r["current_responders"][0]["people"][0]["first_name"] == "Ana"


def test_long_alert_fields_are_clipped(client):
    r = tools.get_incident_context(client, "Q00000000001")
    trace = r["alerts"]["shown"][1]["details"]["trace"]
    assert len(trace) < 400 and "more chars" in trace


def test_unknown_incident_is_not_found_with_next_step(client):
    r = tools.get_incident_context(client, "QDOESNOTEXIST")
    assert r["status"] == "not_found" and "list_open_incidents" in r["next_step"]


def test_unknown_service_id_is_not_found(client):
    assert tools.resolve_service(client, "PZZZZZZ")["status"] == "not_found"


# ---- client policies -----------------------------------------------------------

def test_rate_limit_is_retried_using_reset_header(client, fake):
    fake.fail_next = [(429, {"ratelimit-reset": "2"})]
    r = tools.find_services(client, "Search")
    assert r["count"] == 1 and client.sleeps == [2.0]


def test_server_errors_retry_then_give_up_with_hint(client, fake):
    fake.fail_next = [(503, {})] * 4
    with pytest.raises(PagerDutyError) as e:
        tools.find_services(client, "Search")
    assert e.value.retryable and len(client.sleeps) == 3 and "degraded" in str(e.value)


def test_bad_token_is_not_retried():
    fake = FakePagerDuty()
    sleeps = []
    c = PagerDutyClient("wrong", transport=fake.transport(), sleep=sleeps.append)
    with pytest.raises(PagerDutyError) as e:
        tools.find_services(c, "x")
    assert e.value.status == 401 and sleeps == [] and "Do not retry" in str(e.value)


def test_pagination_cap_marks_truncation(client):
    items, truncated = client.paginate("/incidents", "incidents", {"statuses[]": ["acknowledged"]}, max_items=10)
    assert len(items) == 10 and truncated is True
