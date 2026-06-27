"""Ticket reminders that should stay on the active read path."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .db import open_db

MAX_MUST_REMEMBER_ITEMS = 16


def must_remember_items_from_raw(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [str(raw)] if str(raw or "").strip() else []
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    value = str(parsed).strip()
    return [value] if value else []


def must_remember_items(conn: sqlite3.Connection) -> list[str]:
    row = conn.execute("SELECT must_remember FROM current_view WHERE singleton = 1").fetchone()
    if row is None:
        return []
    return must_remember_items_from_raw(row["must_remember"])


def must_remember_payload_for_ticket(ticket_raw: str) -> dict[str, Any] | None:
    try:
        ticket_dir = Path(ticket_raw).expanduser().resolve()
        conn = open_db(ticket_dir)
        try:
            items = must_remember_items(conn)
        finally:
            conn.close()
    except (Exception, SystemExit):  # noqa: BLE001 - reminders must not break primary commands.
        return None
    return {
        "ticket": str(ticket_dir),
        "count": len(items),
        "limit": MAX_MUST_REMEMBER_ITEMS,
        "items": items,
    }


def _format_must_remember(payload: dict[str, Any]) -> str:
    ticket_dir = Path(str(payload["ticket"]))
    items = [str(item) for item in payload.get("items", [])]
    count = int(payload.get("count", len(items)))
    limit = int(payload.get("limit", MAX_MUST_REMEMBER_ITEMS))
    lines = [
        f"must remember: ticket has {count}/{limit} entries; keep these in the active preflight path.",
        f"  source: {ticket_dir / 'TICKET.md'} section '## Must remember'",
    ]
    lines.extend(f"  {idx}. {item}" for idx, item in enumerate(items, start=1))
    return "\n".join(lines)


def emit_must_remember_reminder(ticket_raw: str, *, fmt: str = "plain") -> None:
    if fmt == "json":
        return
    payload = must_remember_payload_for_ticket(ticket_raw)
    if payload is None or int(payload["count"]) <= 0:
        return
    print(_format_must_remember(payload), file=sys.stderr)
    print(file=sys.stderr)
