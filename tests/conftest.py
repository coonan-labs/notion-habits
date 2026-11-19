# Shared fixtures for the offline unit test suite.
#
# Nothing in this file touches the network. The live Notion tests live in
# test_notion_integration.py, are marked ``integration`` and are skipped by
# default (see pytest.ini). Everything else runs offline against the JSON
# fixtures in tests/fixtures/ and unittest.mock.
import copy
import json
import os
from pathlib import Path
from unittest.mock import Mock

# PyPI imports
import pytest

# Local imports
from src.notion import NotionClient, NotionDataSource

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def api_key() -> str:
    """API key for offline tests: the real one when set, else a dummy.

    Offline tests never send this key anywhere; it only satisfies the
    NotionClient constructor.
    """
    return os.getenv("NOTION_API_KEY") or "ntn_test_dummy_key"


@pytest.fixture
def real_api_key() -> str | None:
    """Real API key, or None when it is not set (integration tests skip)."""
    return os.getenv("NOTION_API_KEY") or None


@pytest.fixture
def page_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "parent": {
                "type": "object",
                "properties": {
                    "data_source_id": {
                        "type": "string"
                    }
                }
            },
            "properties": {
                "type": "object"
            }
        }
    }


@pytest.fixture(params=("daily", "weekly", "monthly"))
def page_type(request) -> str:
    return request.param


@pytest.fixture
def load_fixture():
    """Load a JSON fixture from tests/fixtures/ (fresh copy each call)."""
    def _load(name: str) -> dict:
        with open(FIXTURES_DIR / name, encoding="utf-8") as handle:
            return json.load(handle)
    return _load


@pytest.fixture
def mock_response():
    """Build a mock ``requests.Response`` returning the given payload."""
    def _make(payload=None, status: int = 200, text: str = "",
              headers: dict | None = None) -> Mock:
        response = Mock()
        response.status_code = status
        response.ok = 200 <= status < 300
        response.text = text or ("" if payload is None else json.dumps(payload))
        response.headers = headers or {}
        response.json.return_value = copy.deepcopy(payload)
        return response
    return _make


@pytest.fixture
def make_client(mock_response):
    """Build a NotionClient whose session returns canned payloads.

    Pass a mapping of URL substring -> payload (or a list of payloads,
    consumed in order, for sequential calls to the same endpoint).
    Any request to an unmapped URL fails the test, so a test cannot
    silently pass while making an unexpected call.
    """
    def _make(route_map: dict, api_key: str = "ntn_test_dummy_key") -> NotionClient:
        queues = {
            endpoint: (list(payloads) if isinstance(payloads, list) else [payloads])
            for endpoint, payloads in route_map.items()
        }
        client = NotionClient(api_key=api_key)
        session = Mock()
        calls: list[tuple[str, str]] = []

        def _request(url, method, json=None, timeout=None, **kwargs):
            calls.append((method, url))
            # Longest endpoint first, so e.g. a query URL matches its
            # query route before the shorter data-source-details route.
            for endpoint, queue in sorted(
                    queues.items(), key=lambda item: len(item[0]),
                    reverse=True):
                if endpoint in url:
                    if not queue:
                        raise AssertionError(
                            f"No canned response left for {method} {url}")
                    payload = queue.pop(0)
                    if isinstance(payload, Mock):
                        return payload
                    return mock_response(payload)
            raise AssertionError(f"Unexpected request: {method} {url}")

        session.request.side_effect = _request
        session.calls = calls  # type: ignore[attr-defined]
        client._session = session
        return client
    return _make


@pytest.fixture
def make_data_source(make_client, load_fixture):
    """Build a real NotionDataSource from a data-source fixture file."""
    def _make(fixture_name: str) -> NotionDataSource:
        payload = load_fixture(fixture_name)
        client = make_client({f"/data_sources/{payload['id']}": payload})
        return NotionDataSource(client=client, id=payload["id"])
    return _make


@pytest.fixture
def daily_data_source(make_data_source) -> NotionDataSource:
    return make_data_source("data_source_daily.json")


@pytest.fixture
def weekly_data_source(make_data_source) -> NotionDataSource:
    return make_data_source("data_source_weekly.json")


@pytest.fixture
def analytics_data_source(make_data_source) -> NotionDataSource:
    return make_data_source("data_source_analytics.json")
