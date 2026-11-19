"""
run.py

Script to add habit record pages to Notion databases.
"""

# Base imports
import argparse
import logging
import sys

# PyPI imports
import requests

# Local imports
from src import habits
from src.notion import NotionException

# Set INFO logging for terminal
logging.basicConfig(level=logging.INFO)
logger: logging.Logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Get command line arguments for script

    Args:
        argv (list[str], optional): Arguments to parse, for testing.
            Defaults to None, meaning ``sys.argv``.

    Returns:
        argparse.Namespace: Parsed command line arguments
    """
    # Create argument parser for command line options
    parser = argparse.ArgumentParser(
        description="Utility to add habit record pages to Notion databases.")
    parser.add_argument(
        "--type", type=str, help="Type of habit record to add to Notion",
        required=True, choices=("daily", "weekly", "monthly"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point to execute script

    Args:
        argv (list[str], optional): Arguments to parse, for testing.

    Returns:
        int: Exit code, 0 on success and 1 on a handled failure.
    """
    # Get record type argument
    args = parse_args(argv)
    # Get habit page based on type provided
    page_type = args.type
    try:
        record = habits.get_habit_page(page_type)
        record.commit()
    except (OSError, LookupError, NotionException, requests.RequestException) as exc:
        logger.error("Failed to create %s habit record: %s", page_type, exc)
        return 1
    logger.info(f"Created {page_type} habit record: {record.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
