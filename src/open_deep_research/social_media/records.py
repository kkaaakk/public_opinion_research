"""Parse dates shared by normalized social records and their query boundaries."""

from datetime import date, datetime


def parse_date(value: str | None) -> date | None:
    """Parse an optional ISO date boundary without converting its timezone."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(str(value), "%Y-%m-%d").date()
        except ValueError:
            return None


def parse_datetime(value: str | None) -> datetime | None:
    """Parse a post timestamp, retaining the adapters' naive wall-clock order.

    Choosing ``published_at`` or a source-specific fallback belongs to each
    adapter. Removing tzinfo intentionally preserves the established local-date
    filtering and ordering contract; this does not convert timestamps to UTC.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except ValueError:
        return None
