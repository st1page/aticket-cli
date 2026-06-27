"""Timestamp helpers."""
from __future__ import annotations

from datetime import datetime


def now_iso() -> str:
    """Local timezone-aware ISO timestamp, second precision."""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def iso_from_posix_seconds(ts: float) -> str:
    """Local timezone-aware ISO timestamp from a POSIX timestamp, second precision."""
    return datetime.fromtimestamp(ts).astimezone().replace(microsecond=0).isoformat()


def utc_now_iso() -> str:
    """UTC ISO timestamp with trailing Z, second precision."""
    from datetime import timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def compact_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")
