"""Read-side ticket brief used at stage boundaries and compaction recovery."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .db import open_db
from .notice_alert import unread_message_payload
from .paths import normalize_lifecycle_state
from .reminders import MAX_MUST_REMEMBER_ITEMS, must_remember_items


def _count_bullets(body: str | None) -> int:
    if not body:
        return 0
    return sum(1 for line in body.splitlines() if line.lstrip().startswith("- "))


def _parse_items(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [str(raw)] if str(raw or "").strip() else []
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [str(parsed)] if parsed else []


def _ticket_name(ticket_dir: Path) -> str:
    return ticket_dir.name


def _brief_payload(ticket_dir: Path) -> dict:
    conn = open_db(ticket_dir)
    try:
        meta = conn.execute("SELECT * FROM ticket_meta LIMIT 1").fetchone()
        cv = conn.execute("SELECT * FROM current_view WHERE singleton = 1").fetchone()
        if meta is None or cv is None:
            raise SystemExit("database is missing ticket_meta or current_view row")
        owner = ""
        owner_action = str(meta["owner_last_action"] or "").strip()
        owner_actor = str(meta["owner_last_actor_label"] or "").strip()
        owner_at = str(meta["owner_last_action_at"] or "").strip()
        if owner_action and owner_actor and owner_at:
            owner = f"{owner_action} by {owner_actor} at {owner_at}"
        messages = unread_message_payload(conn)
        items = _parse_items(cv["items"])
        must = must_remember_items(conn)
        return {
            "ticket": _ticket_name(ticket_dir),
            "path": str(ticket_dir),
            "lifecycle": normalize_lifecycle_state(str(meta["lifecycle_state"] or "ACTIVE")),
            "owner": owner,
            "goal": str(cv["goal"] or "").strip(),
            "short_context": str(cv["short_context"] or "").strip(),
            "must_remember": {
                "count": len(must),
                "limit": MAX_MUST_REMEMBER_ITEMS,
                "items": must,
            },
            "unread_messages": messages,
            "counts": {
                "items": len(items),
                "artifacts": _count_bullets(cv["artifacts_md"]),
                "work_log": _count_bullets(cv["work_log_md"]),
            },
            "items": items,
        }
    finally:
        conn.close()


def _format_plain(payload: dict) -> str:
    lines = [
        f"ticket: {payload['ticket']}",
        f"  path:            {payload['path']}",
        f"  lifecycle:       {payload['lifecycle']}",
        f"  owner:           {payload['owner'] or '(unset)'}",
        f"  goal:            {payload['goal'] or '(unset)'}",
        f"  short_context:   {payload['short_context'] or '(unset)'}",
        (
            "  must remember:   "
            f"{payload['must_remember']['count']}/{payload['must_remember']['limit']}"
        ),
    ]
    for idx, item in enumerate(payload["must_remember"]["items"], start=1):
        lines.append(f"    {idx}. {item}")
    unread = payload["unread_messages"]
    lines.append(f"  unread messages: {unread['count']}")
    for message in unread["messages"]:
        metadata = " ".join(message["metadata"])
        suffix = f" {metadata}" if metadata else ""
        lines.append(f"    - [message #{message['id']}] {message['created_at']} {message['message']}{suffix}")
    if unread["truncated"]:
        lines.append("    - ... more unread messages; read TICKET.md for the full list")
    counts = payload["counts"]
    lines.extend([
        f"  items:           {counts['items']}",
        f"  artifacts:       {counts['artifacts']}",
        f"  work log:        {counts['work_log']}",
    ])
    return "\n".join(lines)


def cmd_brief(args: argparse.Namespace) -> int:
    ticket_dir = Path(args.ticket).expanduser().resolve()
    if not ticket_dir.is_dir():
        raise SystemExit(f"ticket dir not found: {ticket_dir}")
    payload = _brief_payload(ticket_dir)
    if getattr(args, "format", "plain") == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(_format_plain(payload))
    return 0
