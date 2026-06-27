"""Unread message reminders for ticket-targeting CLI calls."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .db import open_db

_MESSAGE_PREVIEW_LIMIT = 5


def _notice_metadata(notice: sqlite3.Row) -> list[str]:
    metadata: list[str] = []
    from_ticket = str(notice["from_ticket"] or "").strip()
    if from_ticket:
        metadata.append(f"from={from_ticket}")
    raw_with_items = str(notice["with_items_json"] or "[]")
    try:
        with_items = [str(item) for item in json.loads(raw_with_items) if str(item).strip()]
    except json.JSONDecodeError:
        with_items = []
    metadata.extend(f"with={item}" for item in with_items)
    sender = str(notice["sender"] or "").strip()
    if sender:
        metadata.append(f"by={sender}")
    return metadata


def unread_message_payload(conn: sqlite3.Connection, *, limit: int = _MESSAGE_PREVIEW_LIMIT) -> dict[str, Any]:
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM notices WHERE checked_at IS NULL AND archived_delivery = 0"
    ).fetchone()["n"]
    rows = conn.execute(
        """SELECT * FROM notices
           WHERE checked_at IS NULL AND archived_delivery = 0
           ORDER BY id ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    messages = [
        {
            "id": int(row["id"]),
            "created_at": str(row["created_at"] or ""),
            "message": str(row["message"] or ""),
            "metadata": _notice_metadata(row),
        }
        for row in rows
    ]
    return {
        "count": int(total),
        "messages": messages,
        "truncated": int(total) > len(messages),
    }


def unread_message_payload_for_ticket(ticket_raw: str) -> dict[str, Any] | None:
    """Best-effort unread message payload for a ticket path."""
    try:
        ticket_dir = Path(ticket_raw).expanduser().resolve()
        conn = open_db(ticket_dir)
        try:
            payload = unread_message_payload(conn)
        finally:
            conn.close()
    except (Exception, SystemExit):  # noqa: BLE001 - message reminders must not break the primary command.
        return None
    payload["ticket"] = str(ticket_dir)
    return payload


def attach_unread_messages_to_json(stderr_text: str, ticket_raw: str) -> str:
    """Inject unread messages into a successful JSON command's stderr payload.

    JSON-mode ticket commands write their machine payload to stderr. The CLI
    dispatcher calls this after the command returns so unread messages are a
    post-command aspect instead of per-command reporter behavior.
    """
    payload = unread_message_payload_for_ticket(ticket_raw)
    if payload is None or int(payload["count"]) <= 0:
        return stderr_text
    try:
        data = json.loads(stderr_text)
    except json.JSONDecodeError:
        return stderr_text
    if not isinstance(data, dict):
        return stderr_text
    data["unread_messages"] = payload
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _format_warning(ticket_dir: Path, payload: dict[str, Any]) -> str:
    count = int(payload["count"])
    noun = "message" if count == 1 else "messages"
    lines = [
        f"message: ticket has {count} unread {noun}; read {ticket_dir / 'TICKET.md'} section '## Messages'.",
        f"  after handling: aticket-cli ticket \"{ticket_dir}\" message checked --until-id <message-id>",
    ]
    for item in payload["messages"]:
        metadata = " ".join(item["metadata"])
        suffix = f" {metadata}" if metadata else ""
        lines.append(f"  - [message #{item['id']}] {item['created_at']} {item['message']}{suffix}")
    if payload["truncated"]:
        lines.append("  - ... more unread messages; read TICKET.md for the full list")
    return "\n".join(lines)


def emit_unread_message_warning(ticket_raw: str, *, fmt: str = "plain") -> None:
    """Best-effort stderr reminder after successful ticket-targeting commands."""
    if fmt == "json":
        return
    payload = unread_message_payload_for_ticket(ticket_raw)
    if payload is None:
        return
    if int(payload["count"]) <= 0:
        return
    print(_format_warning(Path(str(payload["ticket"])), payload), file=sys.stderr)
    print(file=sys.stderr)
