# Agent Context: notion-habits

## Project Overview

**notion-habits** is a Python automation tool that creates daily, weekly, and monthly habit tracker records (pages) in Notion databases. It runs on a schedule via GitHub Actions.

## Architecture

```
notion-habits/
├── run.py                          # CLI entry point (--type daily|weekly|monthly)
├── src/
│   ├── habits.py                   # Habit record creation logic and DB configuration
│   └── notion.py                   # Notion API client, data classes, field types
├── tests/
│   ├── conftest.py                 # pytest fixtures (api_key, page_schema, page_type)
│   └── test_notion.py              # Integration tests against real Notion API
├── .github/workflows/add_habits.yml # GitHub Actions schedule (daily 3 UTC)
├── Pipfile                         # Python 3.12 dependencies
├── example.env                     # Environment variable template
└── gascript/                       # Legacy Google Apps Script implementation
```

## Key Modules

### `src/notion.py`
Core Notion API wrapper. Key classes:
- **`NotionClient`**: Authenticates with `NOTION_API_KEY`, wraps API requests. `get_data_source(data_source_name=...)` finds a database by name.
- **`NotionDataSource`**: Represents a Notion database. `.query(params={...})` queries rows; `.new_record(name=...)` creates a new record instance.
- **`NotionRecord`**: Represents a page/row. Fields are accessed as attributes (snake_case). `.commit()` writes to Notion via POST (new) or PATCH (update).
- **`NotionField`** / **`FieldType`**: Handle field type detection and API serialization. Field names are converted to snake_case slugs via `get_slug()`.

API base URL: `https://api.notion.com/v1`, version `2025-09-03`.

### `src/habits.py`
- `RECORD_TYPES`: Maps `"daily"`, `"weekly"`, `"monthly"` to their Notion parent database names and title prefixes.
- `ANALYTICS_DB_NAME`: `"Habit Analytics"` — a summary/rollup database linked from each habit record.
- `get_habit_page(page_type)`: Queries the analytics DB for a summary page, creates a new record in the appropriate habit DB with today's date and a relation to the summary. For `weekly`, also fetches the prior week's record and copies `days_prayed`.

### `run.py`
Parses `--type` argument, calls `get_habit_page()`, calls `.commit()` on the result, and logs the created page ID.

## Environment Variables

| Variable | Description |
|---|---|
| `NOTION_API_KEY` | Notion integration internal secret (format: `ntn_...`) |

- Copy `example.env` to `.env` for local development — Pipenv loads it automatically.
- In GitHub Actions, the secret is stored under the `notion` environment and injected as `NOTION_API_KEY`.

## Notion Database Structure

The integration expects these databases to exist in the connected Notion workspace:

| Database Name | Record type | Description |
|---|---|---|
| `Daily Disciplines` | `daily` | Daily habit tracking rows |
| `Weekly Disciplines` | `weekly` | Weekly habit tracking rows |
| `Monthly Disciplines` | `monthly` | Monthly habit tracking rows |
| `Habit Analytics` | n/a | Summary/rollup pages linked from habit records |

Each habit database page has at minimum: a title, a date field, and a relation to `Habit Analytics`. Weekly records additionally have a relation to the prior week and a `days_prayed` numeric field.

## GitHub Actions Schedule

`.github/workflows/add_habits.yml` runs daily at 03:00 UTC:
- **Every day**: creates a daily habit record
- **Mondays only**: also creates a weekly habit record
- **1st of the month**: also creates a monthly habit record

Can be triggered manually via `workflow_dispatch`.

## Development Workflow

```sh
# Install dependencies
pipenv install --dev

# Set up environment
cp example.env .env
# Edit .env and add your NOTION_API_KEY

# Run locally
pipenv run python run.py --type daily

# Run tests (requires live NOTION_API_KEY in environment)
pipenv run pytest tests/ --cov=src/
```

## Testing

Most tests are offline unit tests and need no `NOTION_API_KEY`. They mock
the Notion API at the `requests.Session` level (`tests/conftest.py`
`make_client`) and use realistic payloads in `tests/fixtures/`
(data-source details, search/query responses, page payloads). Run them with
`pipenv run pytest` — integration tests are deselected by default via
`pytest.ini` (`addopts = -m "not integration"`).

- `tests/test_notion.py`: `get_slug`, `FieldType` detection/parsing,
  `NotionField` serialisation, client request/session/retry/caching,
  data-source query pagination, record parsing/commit.
- `tests/test_habits.py`: `get_habit_page` for daily/weekly/monthly with a
  mocked client, including the weekly prior-week copy and the
  `days_prayed == 0` case. `get_habit_page(page_type, client=..., today=...)`
  accepts an injected client/date so tests never hit the API.
- `tests/test_run.py`: CLI argument validation and exit codes.
- `tests/test_notion_integration.py`: The original live-API tests, marked
  `integration`. Run with
  `NOTION_API_KEY=<key> pytest -m integration -o addopts=''`.
  The `page_type` fixture is parametrized over `daily`, `weekly`, and `monthly`.

CI (`.github/workflows/ci.yml`) runs `pipenv run ruff check .` and then the
offline suite with coverage on every push/PR. Offline coverage is ~99% of
`src/`; the only uncovered lines are pre-existing fallback branches
(rich-text body, formula/rollup parsing). Ruff is a dev dependency in the
Pipfile. Run `pipenv run ruff check .` locally before pushing;
`pipenv run ruff check --fix` applies the safe fixes.

## Coding Conventions

- Python 3.12
- Type annotations on function signatures and module-level constants
- Docstrings use Google style (Args, Raises sections)
- Field names from the Notion API are converted to snake_case via `get_slug()` for attribute access on `NotionRecord`
- Logging via the standard `logging` module at INFO level
