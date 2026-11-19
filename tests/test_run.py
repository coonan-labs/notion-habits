# Offline unit tests for run.py (CLI entry point).
from unittest.mock import Mock, patch

# PyPI imports
import pytest
import requests

# Local imports
import run
from src.notion import NotionException


def test_parse_args_accepts_all_types():
    for page_type in ("daily", "weekly", "monthly"):
        assert run.parse_args(["--type", page_type]).type == page_type


def test_parse_args_rejects_unknown_type():
    # Previously any string was accepted and only failed later in habits.py.
    with pytest.raises(SystemExit) as exc:
        run.parse_args(["--type", "yearly"])
    assert exc.value.code == 2


def test_parse_args_requires_type():
    with pytest.raises(SystemExit) as exc:
        run.parse_args([])
    assert exc.value.code == 2


def test_main_creates_and_commits_record():
    record = Mock()
    record.id = "new-page-id"
    with patch("run.habits.get_habit_page", return_value=record) as get_page:
        assert run.main(["--type", "daily"]) == 0
    get_page.assert_called_once_with("daily")
    record.commit.assert_called_once_with()


@pytest.mark.parametrize("error", [
    EnvironmentError("No Notion API key available"),
    LookupError("Unable to identify record for yearly habits"),
    NotionException("No data source found"),
    requests.RequestException("boom"),
])
def test_main_returns_1_on_handled_failure(error):
    with patch("run.habits.get_habit_page", side_effect=error):
        assert run.main(["--type", "daily"]) == 1


def test_main_returns_1_when_commit_fails():
    record = Mock()
    record.commit.side_effect = NotionException("commit failed")
    with patch("run.habits.get_habit_page", return_value=record):
        assert run.main(["--type", "weekly"]) == 1
