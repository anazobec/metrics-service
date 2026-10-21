"""Segment integration utilities."""

import logging
from pathlib import Path


def read_segment_key_from_path(path: Path) -> str | None:
    """Read SEGMENT_WRITE_KEY from a single file. Returns the key or None."""
    logger = logging.getLogger(__name__)
    try:
        if not path.is_file():
            return None
        key = path.read_text().strip()
        return key if key else None
    except OSError as e:
        filename = getattr(e, "filename", path)
        logger.exception(
            "Failed to read segment write key from %s: %s",
            filename,
            e,
        )
        return None
