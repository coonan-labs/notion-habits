# Offline unit tests for src/notion.py.
#
# No test in this file touches the network: the Notion API is mocked at the
# requests.Session level (see conftest.make_client) and payloads come from
# tests/fixtures/.
import copy
from datetime import date, datetime
from unittest.mock import Mock

# PyPI imports
import pytest
import requests

# Local imports
from src.notion import (
    FieldType,
    NotionClient,
    NotionDataSource,
    NotionException,
    NotionRecord,
    get_slug,
)

GUID = "11111111-1111-4111-8111-111111111111"


# --- get_slug ---------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Name", "name"),
    ("Discipline Analytics", "discipline_analytics"),
    ("Days Prayed", "days_prayed"),
    ("Prior Weekly Discipline", "prior_weekly_discipline"),
    ("My-Field Name!", "my_field_name"),
    ("  Spaced Out  ", "spaced_out"),
])
def test_get_slug(raw, expected):
    assert get_slug(raw) == expected


# --- FieldType.detect / parse ----------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (True, FieldType.CHECKBOX),
    (False, FieldType.CHECKBOX),
    (date(2026, 11, 18), FieldType.DATE),
    (datetime(2026, 11, 18, 3, 0), FieldType.DATE),
    ("hello", FieldType.RICH_TEXT),
    (GUID, FieldType.RELATION),
    (["a", "b"], FieldType.MULTI_SELECT),
    (5, FieldType.NUMBER),
    (1.5, FieldType.NUMBER),
])
def test_field_type_detect(value, expected):
    assert FieldType.detect(value) is expected


@pytest.mark.parametrize("value", [None, {}, {"id": "x"}, object()])
def test_field_type_detect_unsupported_raises(value):
    # Previously detect() returned None here and the failure surfaced
    # later as an obscure AttributeError on a None type.
    with pytest.raises(TypeError):
        FieldType.detect(value)


def test_field_type_phone_number_spelling():
    # Historic misspelling kept as an alias; the correct name is canonical.
    assert FieldType.PHONE_NUMBER.value == "phone_number"
    assert FieldType.PHONE_NUMBER is FieldType.PHOME_NUMBER


@pytest.mark.parametrize("field_type, details, expected", [
    (FieldType.DATE, {"start": "2026-11-18"}, date(2026, 11, 18)),
    (FieldType.DATE, {"start": None}, None),
    (FieldType.DATE, {}, None),
    (FieldType.TITLE, [{"plain_text": "Daily "}, {"plain_text": "Habits"}],
     "Daily Habits"),
    (FieldType.RELATION, [{"id": GUID}], [GUID]),
    (FieldType.NUMBER, 4, 4),
    (FieldType.CHECKBOX, False, False),
])
def test_field_type_parse(field_type, details, expected):
    assert field_type.parse(details) == expected


# --- NotionField ------------------------------------------------------------

def test_notion_field_api_format_by_type(daily_data_source):
    record = daily_data_source.new_record(name="Daily Habits: Nov 18, 2026")
    record.date = date(2026, 11, 18)
    record.discipline_analytics = GUID
    record.prayed = True
    body = record._get_api_body()
    assert body["properties"]["Name"] == {
        "title": [{"text": {"content": "Daily Habits: Nov 18, 2026"}}]}
    assert body["properties"]["Date"] == {"date": {"start": "2026-11-18"}}
    assert body["properties"]["Discipline Analytics"] == {
        "relation": [{"id": GUID}]}
    assert body["properties"]["Prayed"] == {"checkbox": True}
    assert body["parent"] == {
        "data_source_id": "11111111-1111-4111-8111-111111111111"}


def test_notion_field_from_api_preserves_falsy_values(
        weekly_data_source, load_fixture):
    # Regression: the old truthiness check turned 0 / False into None.
    payload = load_fixture("page_prior_week_zero_days.json")
    record = NotionRecord.from_api(parent=weekly_data_source, payload=payload)
    assert record.days_prayed.value == 0
    assert record.asdict()["days_prayed"] == 0


def test_notion_field_from_api_with_null_value(
        daily_data_source, load_fixture):
    payload = load_fixture("page_daily.json")
    payload["properties"]["Prayed"] = {
        "checkbox": None, "id": "chk-prayed", "type": "checkbox"}
    record = NotionRecord.from_api(parent=daily_data_source, payload=payload)
    assert record.prayed.value is None


def test_record_values_and_repr(daily_data_source, load_fixture):
    payload = load_fixture("page_daily.json")
    record = NotionRecord.from_api(parent=daily_data_source, payload=payload)
    assert record.values
    assert "Daily Habits: Nov 18, 2026" in repr(record)
    assert "Prayed" in repr(record.prayed)


def test_notion_field_from_api_preserves_false_checkbox(
        daily_data_source, load_fixture):
    payload = load_fixture("page_daily.json")
    record = NotionRecord.from_api(parent=daily_data_source, payload=payload)
    assert record.prayed.value is False
    assert record.date.value == date(2026, 11, 18)


# --- NotionClient.request ---------------------------------------------------

def test_client_uses_session_with_auth_headers_and_timeout(make_client):
    client = make_client({"/data_sources/": {}})
    assert isinstance(client._session, Mock)  # mocked session in tests
    # The real (unmocked) client still builds a requests.Session with headers.
    real = NotionClient(api_key="ntn_test_key")
    assert real._session.headers["Authorization"] == "Bearer ntn_test_key"
    assert real._session.headers["Notion-Version"] == "2025-09-03"

    client.request("/data_sources/x", "GET")
    _, kwargs = client._session.request.call_args
    assert kwargs["timeout"] == NotionClient.DEFAULT_TIMEOUT == 30


def test_client_request_returns_parsed_json(make_client):
    client = make_client({"/search": {"results": []}})
    assert client.request("/search", "POST", {"query": "x"}) == {"results": []}


def test_client_request_raises_on_http_error(make_client, mock_response):
    error = mock_response(None, status=404, text="not found")
    client = make_client({"/search": [error]})
    with pytest.raises(requests.RequestException):
        client.request("/search", "POST", {})
    # Non-retryable errors fail immediately: exactly one attempt.
    assert client._session.request.call_count == 1


def test_client_retries_rate_limit_then_succeeds(make_client, mock_response):
    limited = mock_response(None, status=429, text="rate limited",
                            headers={"Retry-After": "0"})
    client = make_client({"/search": [limited, {"results": []}]})
    assert client.request("/search", "POST", {}) == {"results": []}
    assert client._session.request.call_count == 2


def test_retry_wait_ignores_invalid_retry_after(mock_response):
    response = mock_response(None, status=429, headers={"Retry-After": "soon"})
    # Falls back to exponential backoff (2 ** (attempt - 1)).
    assert NotionClient._retry_wait(response, 3) == 4.0


def test_retry_wait_uses_retry_after_header(mock_response):
    response = mock_response(None, status=429, headers={"Retry-After": "7"})
    assert NotionClient._retry_wait(response, 1) == 7.0


def test_client_retries_server_error_and_gives_up(make_client, mock_response):
    errors = [mock_response(None, status=503, text="unavailable",
                            headers={"Retry-After": "0"}) for _ in range(3)]
    client = make_client({"/search": errors})
    with pytest.raises(requests.RequestException):
        client.request("/search", "POST", {})
    assert client._session.request.call_count == NotionClient.MAX_RETRIES == 3


# --- NotionClient.get_data_source -------------------------------------------

def test_get_data_source_by_id_loads_once(make_client, load_fixture):
    payload = load_fixture("data_source_daily.json")
    client = make_client({f"/data_sources/{payload['id']}": payload})
    first = client.get_data_source(data_source_id=payload["id"])
    second = client.get_data_source(data_source_id=payload["id"])
    assert first is second
    assert first.title == "Daily Disciplines"
    # Cached: the second lookup made no HTTP call.
    assert client._session.request.call_count == 1


def test_get_data_source_by_name_caches(make_client, load_fixture):
    ds_payload = load_fixture("data_source_daily.json")
    search_payload = load_fixture("search_daily.json")
    client = make_client({
        "/search": search_payload,
        f"/data_sources/{ds_payload['id']}": ds_payload,
    })
    first = client.get_data_source(data_source_name="Daily Disciplines")
    second = client.get_data_source(data_source_name="Daily Disciplines")
    by_id = client.get_data_source(data_source_id=ds_payload["id"])
    assert first is second is by_id
    # One search + one details fetch, despite three lookups.
    assert client._session.request.call_count == 2


def test_get_data_source_by_name_not_found(make_client):
    client = make_client({"/search": {"results": []}})
    with pytest.raises(NotionException, match="No data source found"):
        client.get_data_source(data_source_name="No Such Database")


def test_get_data_source_without_identifier_raises(make_client):
    client = make_client({})
    with pytest.raises(NotionException, match="No data source found"):
        client.get_data_source()


def test_get_data_source_search_ignores_other_titles(make_client, load_fixture):
    ds_payload = load_fixture("data_source_daily.json")
    search_payload = {"results": [
        {"id": "other-id", "title": [{"plain_text": "Other Database"}]},
        {"id": ds_payload["id"],
         "title": [{"plain_text": "Daily Disciplines"}]},
    ]}
    client = make_client({
        "/search": search_payload,
        f"/data_sources/{ds_payload['id']}": ds_payload,
    })
    assert client.get_data_source(
        data_source_name="Daily Disciplines").id == ds_payload["id"]


# --- NotionDataSource -------------------------------------------------------

def test_data_source_properties_are_slugified(daily_data_source):
    props = daily_data_source.properties
    assert set(props) == {"name", "date", "discipline_analytics", "prayed"}
    assert props["discipline_analytics"]["name"] == "Discipline Analytics"


def test_data_source_query_follows_pagination(
        make_client, load_fixture):
    ds_payload = load_fixture("data_source_daily.json")
    page1_payload = load_fixture("page_daily.json")
    page2_payload = copy.deepcopy(page1_payload)
    page2_payload["id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    page2_payload["properties"]["Name"]["title"][0]["plain_text"] = \
        "Daily Habits: Nov 17, 2026"
    page2_payload["properties"]["Name"]["title"][0]["text"]["content"] = \
        "Daily Habits: Nov 17, 2026"
    first_page = {"results": [page1_payload], "has_more": True,
                  "next_cursor": "cursor-1"}
    second_page = {"results": [page2_payload], "has_more": False,
                   "next_cursor": None}
    client = make_client({
        f"/data_sources/{ds_payload['id']}": ds_payload,
        f"/data_sources/{ds_payload['id']}/query": [first_page, second_page],
    })
    data_source = NotionDataSource(client=client, id=ds_payload["id"])
    results = data_source.query(params={"page_size": 1})
    assert [r.id for r in results] == [
        page1_payload["id"], page2_payload["id"]]
    # Second page was requested with the cursor from the first.
    _, second_call = client._session.request.call_args_list[-1]
    assert second_call["json"]["start_cursor"] == "cursor-1"


def test_data_source_query_stops_when_has_more_has_no_cursor(
        make_client, load_fixture):
    ds_payload = load_fixture("data_source_daily.json")
    page_payload = load_fixture("page_daily.json")
    first_page = {"results": [page_payload], "has_more": True,
                  "next_cursor": None}
    client = make_client({
        f"/data_sources/{ds_payload['id']}": ds_payload,
        f"/data_sources/{ds_payload['id']}/query": [first_page],
    })
    data_source = NotionDataSource(client=client, id=ds_payload["id"])
    # Returns the page it has instead of looping forever.
    assert len(data_source.query(params={})) == 1


def test_data_source_query_defaults_to_no_params(
        daily_data_source, load_fixture):
    payload = load_fixture("page_daily.json")
    daily_data_source.client._session.request.side_effect = None
    # Replace the session mock with one returning a single query page.
    response = Mock()
    response.ok = True
    response.json.return_value = {
        "results": [payload], "has_more": False, "next_cursor": None}
    daily_data_source.client._session.request.return_value = response
    results = daily_data_source.query()
    assert len(results) == 1
    assert isinstance(results[0], NotionRecord)


def test_new_record_has_title_field(daily_data_source):
    record = daily_data_source.new_record(name="Daily Habits: Nov 18, 2026")
    assert record.name.value == "Daily Habits: Nov 18, 2026"
    assert record.name.type is FieldType.TITLE
    assert record.id is None


# --- NotionRecord -----------------------------------------------------------

def test_record_from_api_parses_fields(daily_data_source, load_fixture):
    payload = load_fixture("page_daily.json")
    record = NotionRecord.from_api(parent=daily_data_source, payload=payload)
    assert record.id == payload["id"]
    assert record.name.value == "Daily Habits: Nov 18, 2026"
    assert record.date.value == date(2026, 11, 18)
    assert record.discipline_analytics.value == [
        "55555555-5555-4555-8555-555555555555"]
    assert record.asdict()["name"] == "Daily Habits: Nov 18, 2026"
    assert isinstance(record._created_time, datetime)


def test_record_from_api_without_title_raises(
        daily_data_source, load_fixture):
    payload = load_fixture("page_daily.json")
    del payload["properties"]["Name"]
    # Previously this raised a bare IndexError from [0] on an empty list.
    with pytest.raises(NotionException, match="no title property"):
        NotionRecord.from_api(parent=daily_data_source, payload=payload)


def test_record_api_body_rejects_unknown_property(daily_data_source):
    record = daily_data_source.new_record(name="Daily Habits: Nov 18, 2026")
    record.not_a_real_field = "x"
    with pytest.raises(NotionException, match="not_a_real_field"):
        record._get_api_body()


def test_record_commit_creates_new_page(make_client, load_fixture):
    ds_payload = load_fixture("data_source_daily.json")
    new_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    client = make_client({
        f"/data_sources/{ds_payload['id']}": ds_payload,
        "/pages": {"id": new_id},
    })
    data_source = NotionDataSource(client=client, id=ds_payload["id"])
    record = data_source.new_record(name="Daily Habits: Nov 18, 2026")
    record.date = date(2026, 11, 18)
    record.discipline_analytics = GUID
    assert record.commit() == new_id
    assert record.id == new_id
    method, url = client._session.calls[-1]
    assert (method, url) == (
        "POST", f"{NotionClient.BASE_URL}/pages")


def test_record_commit_updates_existing_page(make_client, load_fixture):
    ds_payload = load_fixture("data_source_daily.json")
    page_payload = load_fixture("page_daily.json")
    client = make_client({
        f"/data_sources/{ds_payload['id']}": ds_payload,
        f"/pages/{page_payload['id']}": {"id": page_payload["id"]},
    })
    data_source = NotionDataSource(client=client, id=ds_payload["id"])
    record = NotionRecord.from_api(parent=data_source, payload=page_payload)
    assert record.commit() == page_payload["id"]
    method, url = client._session.calls[-1]
    assert (method, url) == (
        "PATCH", f"{NotionClient.BASE_URL}/pages/{page_payload['id']}")
