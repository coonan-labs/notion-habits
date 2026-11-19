# Offline unit tests for src/habits.py.
#
# The Notion client is a Mock: get_data_source() returns real data sources
# built from fixtures, and their query() methods return records parsed
# from fixture payloads. No network, no NOTION_API_KEY needed.
from datetime import date
from unittest.mock import Mock

# PyPI imports
import pytest
from jsonschema import validate

# Local imports
from src.habits import ANALYTICS_DB_NAME, RECORD_TYPES, get_habit_page
from src.notion import NotionField, NotionRecord

TODAY = date(2026, 11, 18)
SUMMARY_ID = "55555555-5555-4555-8555-555555555555"
PRIOR_WEEK_ID = "88888888-8888-4888-8888-888888888888"


@pytest.fixture
def habits_client(make_data_source, load_fixture):
    """A mock client wired to real fixture-backed data sources."""
    analytics_ds = make_data_source("data_source_analytics.json")
    parent_dss = {
        "Daily Disciplines": make_data_source("data_source_daily.json"),
        "Weekly Disciplines": make_data_source("data_source_weekly.json"),
        "Monthly Disciplines": make_data_source("data_source_monthly.json"),
    }
    summary = NotionRecord.from_api(
        parent=analytics_ds,
        payload=load_fixture("page_summary_daily.json"))
    analytics_ds.query = Mock(return_value=[summary])

    client = Mock()
    client.get_data_source.side_effect = lambda data_source_name=None, **_: (
        analytics_ds if data_source_name == ANALYTICS_DB_NAME
        else parent_dss[data_source_name])
    client.analytics_ds = analytics_ds
    client.parent_dss = parent_dss
    return client


def _set_prior_week(habits_client, fixture_name="page_prior_week.json",
                    load_fixture=None):
    prior = NotionRecord.from_api(
        parent=habits_client.parent_dss["Weekly Disciplines"],
        payload=load_fixture(fixture_name))
    habits_client.parent_dss["Weekly Disciplines"].query = Mock(
        return_value=[prior])
    return prior


@pytest.mark.parametrize("page_type, title", [
    ("daily", "Daily Habits: Nov 18, 2026"),
    ("monthly", "Month: Nov 18, 2026"),
])
def test_get_habit_page_daily_and_monthly(
        habits_client, page_schema, page_type, title):
    record = get_habit_page(page_type, client=habits_client, today=TODAY)

    assert record.name.value == title
    # New records hold plain values (only from_api() wraps them in
    # NotionField); _get_api_body() converts them on serialisation.
    assert record.date == TODAY
    assert record.discipline_analytics == SUMMARY_ID
    assert record.id is None  # not committed yet
    validate(record._get_api_body(), page_schema)
    # Analytics looked up once, parent looked up once.
    assert habits_client.get_data_source.call_count == 2


def test_get_habit_page_weekly_copies_prior_week(
        habits_client, load_fixture, page_schema):
    _set_prior_week(habits_client, load_fixture=load_fixture)

    record = get_habit_page("weekly", client=habits_client, today=TODAY)

    assert record.name.value == "Week: Nov 18, 2026"
    assert record.prior_weekly_discipline == PRIOR_WEEK_ID
    # The copied value is a raw int, not the prior record's NotionField.
    assert record.days_prayed == 4
    assert not isinstance(record.days_prayed, NotionField)
    # Serialises as a plain number, not a nested field object.
    body = record._get_api_body()
    assert body["properties"]["Days Prayed"] == {"number": 4}
    validate(body, page_schema)


def test_get_habit_page_weekly_copies_zero_days_prayed(
        habits_client, load_fixture):
    # Regression: days_prayed == 0 is a real value, not "no value".
    _set_prior_week(habits_client,
                    fixture_name="page_prior_week_zero_days.json",
                    load_fixture=load_fixture)

    record = get_habit_page("weekly", client=habits_client, today=TODAY)

    assert record.days_prayed == 0
    assert record._get_api_body()["properties"]["Days Prayed"] == {"number": 0}


def test_get_habit_page_weekly_without_prior_week_raises(
        habits_client):
    habits_client.parent_dss["Weekly Disciplines"].query = Mock(
        return_value=[])
    # Previously this raised a bare IndexError on results[0].
    with pytest.raises(LookupError, match="No prior weekly record"):
        get_habit_page("weekly", client=habits_client, today=TODAY)


def test_get_habit_page_unknown_type_raises_before_any_api_call(
        habits_client, monkeypatch):
    # Validation happens before credentials / network, so an unknown type
    # fails fast even with no API key and no client calls.
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    with pytest.raises(LookupError, match="Unable to identify record"):
        get_habit_page("yearly")
    habits_client.get_data_source.assert_not_called()


def test_get_habit_page_creates_client_from_env(
        habits_client, monkeypatch):
    # The client=None path still works offline when the key is present:
    # NotionClient is constructed from the env var, then used as normal.
    monkeypatch.setenv("NOTION_API_KEY", "ntn_test_dummy_key")
    monkeypatch.setattr("src.habits.NotionClient",
                        Mock(return_value=habits_client))
    record = get_habit_page("daily", today=TODAY)
    assert record.name.value == "Daily Habits: Nov 18, 2026"


def test_get_habit_page_without_client_or_api_key_raises(monkeypatch):
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    with pytest.raises(EnvironmentError, match="No Notion API key"):
        get_habit_page("daily")


def test_get_habit_page_without_summary_raises(habits_client):
    habits_client.analytics_ds.query = Mock(return_value=[])
    with pytest.raises(LookupError, match="No summary page found"):
        get_habit_page("daily", client=habits_client, today=TODAY)


def test_record_types_cover_all_page_types():
    assert set(RECORD_TYPES) == {"daily", "weekly", "monthly"}
    for config in RECORD_TYPES.values():
        assert config["parent"].endswith("Disciplines")
        assert config["title"]
