"""
habits.py

Module to create Notion records for habit tracker databases.
"""

# Base imports
import os
from datetime import date
from logging import Logger, getLogger

# Local imports
from src.notion import NotionClient, NotionField, NotionRecord

logger: Logger = getLogger(__name__)
RECORD_TYPES: dict = {
    "daily": {
        "parent": "Daily Disciplines",
        "title": "Daily Habits:",
    },
    "weekly": {
        "parent": "Weekly Disciplines",
        "title": "Week:",
    },
    "monthly": {
        "parent": "Monthly Disciplines",
        "title": "Month:",
    },
}
"""dict: Mapping of habit record configurations for page properties"""

ANALYTICS_DB_NAME: str = "Habit Analytics"
"""str: Name of the database for habit analytics"""


def get_habit_page(page_type: str, client: NotionClient | None = None,
                   today: date | None = None) -> NotionRecord:
    """Get a page on the relevant habit database

    Args:
        page_type (str): Daily, weekly or monthly habit page
        client (NotionClient, optional): Client to use. Defaults to None,
            in which case one is created from ``NOTION_API_KEY``.
            Supplying a client (e.g. a mocked one) makes the function
            testable without hitting the Notion API.
        today (date, optional): Date to stamp the record with.
            Defaults to None, meaning ``date.today()``.

    Raises:
        EnvironmentError: Missing Notion API key in environment
        LookupError: Unable to identify page type record for habits
        LookupError: Unable to identify summary page identifier from analytics database
        LookupError: No prior weekly record found (weekly only)
    """
    # Validate period before doing any network / credential work.
    if page_type not in RECORD_TYPES:
        raise LookupError(f"Unable to identify record for {page_type} habits")

    # Connect to Notion API (or use the supplied client)
    if client is None:
        api_key = os.getenv("NOTION_API_KEY")
        if not api_key:
            raise OSError("No Notion API key available")
        client = NotionClient(api_key=api_key)

    # Get and validate parent database ID from environment
    period_record = RECORD_TYPES[page_type]
    parent_db_name = period_record["parent"]
    logger.info(f"Adding habit record to parent database: {parent_db_name}")

    # Connect to Notion data sources
    analytics_ds = client.get_data_source(data_source_name=ANALYTICS_DB_NAME)
    summary_results = analytics_ds.query(
        params={
            "filter": {
                "property": "Name",
                "title": {
                    "starts_with": parent_db_name.split(" ")[0]
                }
            }
        })
    if not summary_results:
        raise LookupError(f"No summary page found for {parent_db_name}")
    summary_page = summary_results[0].id
    data_source = client.get_data_source(data_source_name=parent_db_name)
    # Create instance of Notion record and set date
    today = today or date.today()
    record = data_source.new_record(
        name=f"{period_record['title']} {today.strftime('%b %d, %Y')}")
    record.date = today
    record.discipline_analytics = summary_page
    logger.info(f"Created {page_type} habit record for database")

    # Make weekly-specifc updates
    if page_type == "weekly":
        # For weekly habit tasks, get latest week to link
        results = data_source.query(
            params={
                "page_size": 1,
                "sorts": [
                    {
                        "property": "Date",
                        "direction": "descending",
                    }
                ]
            })
        if not results:
            raise LookupError(
                f"No prior weekly record found in {parent_db_name}")
        # Update record with prior week details, copying the raw value
        # (not the NotionField object) so the new record serialises cleanly.
        prior_week = results[0]
        record.prior_weekly_discipline = prior_week.id
        days_prayed = getattr(prior_week, "days_prayed", None)
        if isinstance(days_prayed, NotionField):
            days_prayed = days_prayed.value
        if days_prayed is not None:
            record.days_prayed = days_prayed

    # Provide page to commit
    return record
