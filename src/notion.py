"""
notion.py

Custom Python client for Notion API to query databases, add records
and manage field interactions.
"""

# Base imports
import re
import time
from logging import getLogger, Logger
from datetime import date, datetime
import enum
from typing import Any

# PyPI imports
import requests

logger: Logger = getLogger(__name__)

_SLUG_STRIP_RE = re.compile(r"[^\w\s-]")
_SLUG_SEP_RE = re.compile(r"[-\s]+")
_GUID_RE = re.compile(r"\w{8}\-\w{4}\-\w{4}\-\w{4}\-\w{12}")

# HTTP status codes worth retrying (rate limiting / transient server errors).
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def get_slug(value: str) -> str:
    """Convert string to snake case property slug

    Args:
        value (str): Field name to convert to slug

    Returns:
        str: Slugified field name for property
    """
    value = _SLUG_STRIP_RE.sub("", value).strip().lower()
    value = _SLUG_SEP_RE.sub("_", value)
    return value


class NotionException(Exception):
    """Custom Notion exception class"""
    pass


class NotionClient:
    """Notion API interface for integrations"""

    BASE_URL = "https://api.notion.com/v1"
    """str: Base link for API endpoints"""
    API_VERSION = "2025-09-03"
    """str: API version to use in requests"""
    METADATA_FIELDS = ["id", "url", "created_time", "last_edited_time"]
    """list[str]: Metadata fields related to all Notion objects"""
    DEFAULT_TIMEOUT = 30
    """int: Seconds to wait for a Notion API response before timing out"""
    MAX_RETRIES = 3
    """int: Total attempts for a request before giving up (1 initial + retries)"""

    def __init__(self, api_key: str, timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = MAX_RETRIES):
        """Constructor for Notion client

        Args:
            api_key (str): Internal secret API key from Notion integration
            timeout (float): Seconds to wait for an API response.
                Defaults to DEFAULT_TIMEOUT.
            max_retries (int): Total attempts for retryable failures.
                Defaults to MAX_RETRIES.
        """
        self.__api_key = api_key
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        # One session per client: reuses TCP/TLS connections and sends the
        # auth headers on every request, instead of a new connection per call.
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.__api_key}",
            "Notion-Version": self.API_VERSION,
        })
        # Cache of data sources already looked up, keyed by id and by name,
        # so a run that needs the same database twice only fetches it once.
        self._data_source_cache: dict[str, "NotionDataSource"] = {}

    def request(self, endpoint: str, method: str, payload: dict = None) -> dict:
        """Make request to Notion API

        Args:
            endpoint (str): Relative URL of Notion API endpoint
            method (str): Method of HTTP request
            payload (dict, optional): Body to provide in API request. Defaults to None.

        Raises:
            requests.RequestException: Unsuccessful HTTP request

        Returns:
            dict: Parsed API response
        """
        url = self.BASE_URL + endpoint
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            response = self._session.request(
                url=url,
                method=method,
                json=payload,
                timeout=self._timeout,
            )
            # Validate and parse response or raise exception
            if response.ok:
                return response.json()
            last_error = requests.RequestException(
                response.text, response=response)
            # Retry rate-limit / transient server errors, honouring any
            # Retry-After header; anything else fails immediately.
            if (response.status_code in _RETRYABLE_STATUS
                    and attempt < self._max_retries):
                wait = self._retry_wait(response, attempt)
                logger.warning(
                    "Notion API %s %s failed (%s), retrying in %.1fs "
                    "(attempt %d/%d)",
                    method, endpoint, response.status_code, wait,
                    attempt, self._max_retries)
                time.sleep(wait)
                continue
            raise last_error
        # Unreachable when max_retries >= 1, but keeps type checkers honest.
        raise last_error  # pragma: no cover

    @staticmethod
    def _retry_wait(response: requests.Response, attempt: int) -> float:
        """Backoff before a retry, honouring Retry-After when present.

        Args:
            response (requests.Response): Failed response.
            attempt (int): Attempt number that just failed (1-based).

        Returns:
            float: Seconds to wait before the next attempt.
        """
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return 2.0 ** (attempt - 1)

    def get_data_source(self, data_source_id: str = None, data_source_name: str = None) -> "NotionDataSource":
        """Get Notion data source object using client integration

        Args:
            data_source_id (str): Identifier for Notion data source
            data_source_name (str): Name of Notion data source

        Returns:
            NotionDataSource: Data source object from Notion page

        Raises:
            NotionException: No data source found with provided identifier or name
        """
        # Return a cached data source when this client already loaded it.
        cache_key = data_source_id or data_source_name
        if cache_key and cache_key in self._data_source_cache:
            return self._data_source_cache[cache_key]

        # Get data source using client and identifier
        if data_source_id:
            data_source = NotionDataSource(client=self, id=data_source_id)
        elif data_source_name:
            result = self.request("/search", "POST", {
                "query": data_source_name,
                "filter": {"property": "object", "value": "data_source"},
            })
            matched_results = [
                d for d
                in result.get("results", [])
                if "".join(t["plain_text"] for t in d.get("title", [])) == data_source_name
            ]
            if matched_results:
                data_source = NotionDataSource(
                    client=self, id=matched_results[0]["id"])
            else:
                raise NotionException(
                    "No data source found with provided identifier or name")
        else:
            raise NotionException(
                "No data source found with provided identifier or name")

        # Cache under both the id and, when known, the name, so a later
        # lookup by either key reuses the loaded schema.
        self._data_source_cache[data_source.id] = data_source
        if data_source_name:
            self._data_source_cache[data_source_name] = data_source
        return data_source


class NotionDataSource:
    """Structured set of Notion pages with defined properties"""

    def __init__(self, client: NotionClient, id: str):
        """Constructor for Notion data source

        Args:
            client (NotionClient): Initialized client with credentials
            id (str): Identifier for data source
        """
        # Set basic attributes
        self._id = id
        self.client = client

        # Load properties from API
        self._load_properties()

    def _load_properties(self):
        """Retrieve and store properties defined for data source"""
        # Get details of data source from API
        details = self.client.request(
            endpoint=f"/data_sources/{self.id}", method="GET")

        # Set metadata fields as object properties
        for field in self.client.METADATA_FIELDS:
            setattr(self, "_" + field, details[field])

        # Parse text-based attributes
        self.title = FieldType.TITLE.parse(details["title"])
        self.description = FieldType.RICH_TEXT.parse(details["description"])

        # Assign properties to attribute
        self._properties = details["properties"]

    @property
    def id(self) -> str:
        """Getting method for ID

        Note:
            No setter method to prevent override

        Returns:
            str: Identifier for data source
        """
        return self._id

    @property
    def properties(self) -> dict[str, str]:
        """Slugified data source properties (schema)

        Returns:
            dict[str, str]: Data source properties
        """
        return {get_slug(k): v for k, v in self._properties.items()}

    def query(self, params: dict = None) -> list["NotionRecord"]:
        """Query a Notion data source with parameters

        Args:
            params (dict, optional): Parameters for data source query.
                Defaults to None (no filter / default page size).

        Notes:
            Define filters for the query request using
            `Notion structure.<https://developers.notion.com/reference/post-data-source-query>`_
            Follows ``has_more`` / ``next_cursor`` pagination so a query
            returns every matching record, not just the first page.

        Returns:
            list[NotionRecord]: Records returned from data source query
        """
        # Query a Notion data source using query parameters
        params = dict(params) if params else {}
        records: list[NotionRecord] = []
        while True:
            results = self.client.request(
                f"/data_sources/{self.id}/query", "POST", params)
            # Parse results to record objects
            records.extend(
                NotionRecord.from_api(parent=self, payload=result)
                for result in results.get("results", [])
            )
            if not results.get("has_more"):
                return records
            # Guard against a malformed page that claims more results
            # but supplies no cursor: returning what we have beats looping.
            if not results.get("next_cursor"):
                logger.warning(
                    "Notion query for %s reported has_more without a "
                    "next_cursor; returning %d records",
                    self.id, len(records))
                return records
            params["start_cursor"] = results.get("next_cursor")

    def new_record(self, name: str) -> "NotionRecord":
        """Create a new record for the data source

        Args:
            name (str): Value for title field of data source

        Returns:
            NotionRecord: Record instance for data source row
        """
        # Get record with name and parent data source reference
        return NotionRecord(name=name, parent=self)


class NotionRecord:
    """Representation of a row page in a data source collection"""

    def __init__(self, name: str, parent: "NotionDataSource"):
        """Constructor for new Data Source Record (row)

        Args:
            name (str): Value for title field of data source
            parent (NotionDataSource): Parent data source for record.
        """
        # Set initial properties
        self._id = None
        self._parent = parent
        # Set name as Notion Field object
        self.name = NotionField(record=self, name="name",
                                value=name, field_type="title")

    def __repr__(self):
        return "<{} ({})>".format(self.__class__.__name__, self.name.value)

    @classmethod
    def from_api(cls, parent: NotionDataSource, payload: dict) -> "NotionRecord":
        """Create instance from API response

        Args:
            parent (NotionDataSource): Data source to which record belongs
            payload (dict): Payload retrieved from Notion API

        Returns:
            NotionRecord: Record created from API response

        Raises:
            NotionException: Payload has no title property
        """
        # Get title property (often 'Name')
        title_property = next(
            (v for v in payload["properties"].values() if v["type"] == "title"),
            None)
        if title_property is None:
            raise NotionException(
                f"Notion page {payload.get('id')} has no title property")
        title_value = FieldType.TITLE.parse(title_property["title"])

        # Create record instance
        record = cls(name=title_value, parent=parent)

        # Create class instance and add metadata attributes
        logger.info(f"Parsing Notion page record: {payload['id']}, {title_value}")
        for field in parent.client.METADATA_FIELDS:
            value = payload[field]
            if field.endswith("_time"):
                value = datetime.fromisoformat(payload[field])
            setattr(record, "_" + field, value)

        # Parse properties from query result payload
        properties = dict()
        for property, details in payload["properties"].items():
            field = NotionField.from_api(
                record=record, name=property, details=details)
            properties[field.name] = field

        # Assign record field properties
        for property, value in properties.items():
            if property != "name":
                setattr(record, property, value)

        return record

    @property
    def id(self) -> str:
        """Getter method for ID property

        Note:
            No setter is specified to avoid override

        Returns:
            str: Record identifier
        """
        return self._id

    @property
    def fields(self) -> dict:
        """Public record fields as dictionary

        Returns:
            dict: Fields and values for record
        """
        return {
            var: val for var, val in vars(self).items() if var[0] != "_"
        }

    def asdict(self) -> dict:
        """Public fields with values returned directly

        Returns:
            dict: Fields with parsed values
        """
        # Get values from fields
        fields = dict()
        for var, val in self.fields.items():
            if isinstance(val, NotionField):
                val = val.value
            fields[var] = val
        return fields

    @property
    def values(self) -> list[Any]:
        """List of field values

        Returns:
            list[Any]: Values from Notion fields
        """
        return list(self.fields.values())

    def _get_api_body(self) -> dict:
        """Structure record data for API requests

        Returns:
            dict: Representation of Notion Record for API requests
        """
        properties = dict()

        # Check for record properties not on data source
        invalid_properties = [
            k for k in self.fields.keys() if k not in self._parent.properties]
        if invalid_properties:
            raise NotionException(f"Record properties do not exist on parent data source: "
                                  f"{', '.join(invalid_properties)}")

        # Format all NotionField properties for API requests
        for var, val in self.fields.items():
            if not isinstance(val, NotionField):
                val = NotionField(record=self, name=var, value=val)
            # Add API-formatted field to body dictionary
            properties.update(val.get_api_format())

        body = {
            "properties": properties,
            "parent": {},
        }
        if self._parent:
            body["parent"]["data_source_id"] = self._parent.id

        return body

    def commit(self) -> str:
        """Update record values in Notion

        Returns:
            str: Identifier from committed record
        """
        # If ID is set (record exists), update
        if self._id:
            result = self._parent.client.request(
                f"/pages/{self._id}", "PATCH", self._get_api_body())
        else:
            # Create new page and retrieve ID
            result = self._parent.client.request(
                "/pages", "POST", self._get_api_body())
            self._id = result["id"]

        return self._id


class NotionField:
    """Field in Notion with name, type and value"""

    def __init__(self, record: NotionRecord, name: str, value: Any, field_type: str = None):
        """Constructor for NotionField object

        Args:
            record (NotionRecord): Record to which field belongs
            name (str): Name of field in the Notion collection
            value (Any): Value of Notion field
            field_type (str, optional): Type of Notion field, detected if not specified.
        """
        # Assign basic attributes
        self._record = record
        self.name = name
        # Detect field type if not specified
        if field_type:
            self.type = FieldType(field_type)
        else:
            self.type = FieldType.detect(value)
        self.value = value

    def __repr__(self):
        return "<{} ({})>".format(self.__class__.__name__, self.display_name)

    @classmethod
    def from_api(cls, record: NotionRecord, name: str, details: dict) -> "NotionField":
        """Create NotionField instance from API response

        Args:
            record (NotionRecord): Record to which field belongs
            name (str): Name of Notion Field
            details (dict): Details from API response

        Returns:
            NotionField: Instance of Notion Field
        """
        # Convert name to snake case
        name = get_slug(name)
        # Get field type to parse value
        field_type = FieldType(details["type"])
        # `is not None` (not truthiness): 0 and False are real values.
        if details[field_type.value] is not None:
            field_value = field_type.parse(details[field_type.value])
        else:
            field_value = None
        # Return class instance
        return cls(record=record, name=name, value=field_value, field_type=field_type)

    def get_api_format(self) -> dict:
        """Return API format dictionary for request

        Returns:
            dict: Mapping of display name and API body
        """
        return {
            self.display_name: self._api_body,
        }

    @property
    def display_name(self) -> str:
        """Display name of field within interface

        Returns:
            str: Formatted field name
        """
        return self._record._parent.properties[self.name]["name"]

    @property
    def _api_body(self) -> dict:
        """Generate API body structure for requests

        Returns:
            dict: API structure for field based on type
        """
        if self.type == FieldType.TITLE:
            return {
                "title": [
                    {
                        "text": {
                            "content": self.value,
                        }
                    }
                ]
            }
        elif self.type == FieldType.RICH_TEXT:
            return {
                "rich_text": [
                    {
                        "text": {
                            "content": self.value,
                        }
                    }
                ]
            }
        elif self.type == FieldType.DATE:
            return {
                "date": {
                    "start": self.value.isoformat(),
                }
            }
        elif self.type == FieldType.RELATION:
            return {
                "relation": [{
                    "id": self.value,
                }]
            }
        else:
            # fallback to type: value
            return {
                self.type.value: self.value,
            }


class FieldType(enum.Enum):
    """Notion field types and conversion methods"""

    CHECKBOX = "checkbox"
    CREATED_BY = "created_by"
    CREATED_TIME = "created_time"
    DATE = "date"
    EMAIL = "email"
    ICON = "icon"
    FILES = "files"
    FORMULA = "formula"
    LAST_EDITED_BY = "last_edited_by"
    LAST_EDITED_TIME = "last_edited_time"
    MULTI_SELECT = "multi_select"
    NUMBER = "number"
    PHONE_NUMBER = "phone_number"
    # Backwards-compatible alias for the historic misspelling.
    PHOME_NUMBER = "phone_number"
    RELATION = "relation"
    RICH_TEXT = "rich_text"
    ROLLUP = "rollup"
    SELECT = "select"
    TITLE = "title"
    URL = "url"

    @classmethod
    def detect(cls, value: Any) -> "FieldType":
        """Determine type of Notion field by value

        Args:
            value (Any): Value of field to detect from

        Returns:
            FieldType: Enum instance value matching value

        Raises:
            TypeError: Value has no detectable Notion field type
                (e.g. ``None`` or a dict).
        """
        # Return enum value based on variable type.
        # bool must be checked before int: bool is a subclass of int.
        if isinstance(value, bool):
            return cls.CHECKBOX
        if isinstance(value, (date, datetime)):
            return cls.DATE
        if isinstance(value, str):
            # Detect GUID format used for links
            if _GUID_RE.match(value):
                return cls.RELATION
            return cls.RICH_TEXT
        if isinstance(value, list):
            return cls.MULTI_SELECT
        if isinstance(value, (int, float)):
            return cls.NUMBER
        raise TypeError(
            f"Cannot detect Notion field type for value of type "
            f"{type(value).__name__}: {value!r}")

    def parse(self, details: dict | bool) -> Any:
        """Parse Notion API payload based on field type

        Args:
            details (dict | bool): Field details returned from API

        Returns:
            Any: Value of parsed field with type converted
        """
        # Parse values based on field type
        if self.value == "date":
            if not details or not details.get("start"):
                return None
            return date.fromisoformat(details["start"])
        elif self.value == "created_by":
            return details["id"]
        elif self.value in ("title", "rich_text"):
            return "".join(t["plain_text"] for t in details)
        elif self.value == "relation":
            return [r["id"] for r in details]
        elif self.value in ("formula", "rollup"):
            return details[details["type"]]
        else:
            # Simple values or adapters not identified
            return details
