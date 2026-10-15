"""Offline package checks; no migrated business capability is implied."""

VERSION = "0.1.0"
DOMAINS = ("rag", "research_graph", "social_media", "sentiment", "risk", "evidence", "reports")


def ping(text: str) -> dict[str, str]:
    """Echo bounded text to verify the runtime → extension → business boundary."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    normalized = text.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError("text must contain 1 to 200 characters after trimming")
    return {"status": "ok", "phase": "phase0", "version": VERSION, "text": normalized}


def get_status() -> dict[str, object]:
    """Describe Phase 0 honestly: health is implemented, business domains are interfaces."""
    return {
        "status": "ok",
        "phase": "phase0",
        "version": VERSION,
        "domains": dict.fromkeys(DOMAINS, "interface_only"),
    }
