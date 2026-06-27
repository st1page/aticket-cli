"""External message inbox for tickets.

The sqlite table is still named `notices` for on-disk compatibility. The public
surface is `message`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import identity
from .db import open_db
from .lifecycle import _escape_inline_breaks, require_not_archived, resolve_ticket
from .render import do_render
from .reporter import WriteChange, emit_post_write
from .search_index import safe_upsert_ticket
from .paths import normalize_lifecycle_state
from .timeutil import now_hms, now_iso

_ARCHIVED_DELIVERY_WARNING = "message appended to archived ticket; no active holder/session is guaranteed to receive it"


def _ticket_file_uri(raw: str) -> str:
    p = Path(raw).expanduser().resolve()
    if not p.is_dir():
        raise SystemExit(f"from-ticket dir not found: {p}")
    return p.as_uri()


def _clean_with_items(raw_items: list[str]) -> list[str]:
    out: list[str] = []
    for raw in raw_items:
        item = str(raw).strip()
        if not item:
            continue
        if "\n" in item or "\r" in item:
            raise SystemExit("error: message --with values must be single-line URI items")
        out.append(item)
    return out

def _clean_message(raw: str) -> str:
    message = _escape_inline_breaks(str(raw or "")).strip()
    if not message:
        raise SystemExit("error: message send requires a non-empty positional message")
    return message


def _notice_metadata(notice) -> list[str]:
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


def _notice_log_line(notice) -> str:
    parts = [f"{now_hms()}: [message #{notice['id']}] {notice['message']}"]
    parts.extend(_notice_metadata(notice))
    return " ".join(parts)


def _append_bullet(existing: str | None, line: str) -> str:
    bullet = f"- {line}"
    body = (existing or "").rstrip()
    return bullet if not body else f"{body}\n{bullet}"


def _count_bullets(body: str | None) -> int:
    if not body:
        return 0
    return sum(1 for line in body.splitlines() if line.lstrip().startswith("- "))


def _require_not_archived_in_tx(conn, *, action: str) -> None:
    meta = conn.execute("SELECT lifecycle_state FROM ticket_meta LIMIT 1").fetchone()
    if meta is not None and normalize_lifecycle_state(str(meta["lifecycle_state"] or "ACTIVE")) == "ARCHIVED":
        raise SystemExit(
            f"refuse to {action}: ticket is ARCHIVED and cannot be modified; "
            "fork it or create a new ticket instead"
        )


def _require_message_send_not_archived(ticket_dir: Path) -> None:
    conn = open_db(ticket_dir)
    try:
        meta = conn.execute("SELECT lifecycle_state FROM ticket_meta LIMIT 1").fetchone()
    finally:
        conn.close()
    if meta is not None and normalize_lifecycle_state(str(meta["lifecycle_state"] or "ACTIVE")) == "ARCHIVED":
        raise SystemExit(
            "refuse to send message: ticket is ARCHIVED and cannot receive active messages; "
            "use --allow-archived to append historical context with no delivery guarantee"
        )


def _require_active_holder_in_tx(conn, holder: identity.HolderIdentity, *, action: str) -> None:
    meta = conn.execute("SELECT owner_id, owner_label FROM ticket_meta LIMIT 1").fetchone()
    if meta is None:
        raise SystemExit("database is missing ticket_meta row")
    owner_id = str(meta["owner_id"] or "").strip()
    owner_label = str(meta["owner_label"] or "").strip()
    if not owner_id:
        raise SystemExit(f"refuse to {action}: ticket is not currently claimed")
    if owner_id != holder.holder_id:
        raise SystemExit(
            f"refuse to {action}: ticket is claimed by {owner_label or owner_id}; "
            "only the active holder can mark messages checked"
        )


def cmd_notice_send(args: argparse.Namespace) -> int:
    ticket_dir = resolve_ticket(args.ticket)
    allow_archived = bool(getattr(args, "allow_archived", False))
    if not allow_archived:
        _require_message_send_not_archived(ticket_dir)
    message = _clean_message(getattr(args, "message", "") or "")

    holder = identity.infer_holder(
        agent_type=getattr(args, "agent_type", ""),
        session_id=getattr(args, "session_id", ""),
        explicit_label=getattr(args, "owner_label", ""),
    )
    from_ticket = _ticket_file_uri(args.from_ticket) if getattr(args, "from_ticket", "") else ""
    with_items = _clean_with_items(getattr(args, "with_items", None) or [])

    conn = open_db(ticket_dir)
    try:
        now = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        meta = conn.execute("SELECT lifecycle_state FROM ticket_meta LIMIT 1").fetchone()
        lifecycle_state = "ACTIVE"
        if meta is not None:
            lifecycle_state = normalize_lifecycle_state(str(meta["lifecycle_state"] or "ACTIVE"))
        archived_delivery = 0
        if lifecycle_state == "ARCHIVED":
            if not allow_archived:
                raise SystemExit(
                    "refuse to send message: ticket is ARCHIVED and cannot receive active messages; "
                    "use --allow-archived to append historical context with no delivery guarantee"
                )
            archived_delivery = 1
        cur = conn.execute(
            """INSERT INTO notices
               (created_at, message, from_ticket, with_items_json, sender, sender_label, archived_delivery)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                now,
                message,
                from_ticket,
                json.dumps(with_items),
                holder.holder_id,
                holder.holder_label,
                archived_delivery,
            ),
        )
        conn.execute(
            "UPDATE ticket_meta SET render_revision = render_revision + 1, updated_at = ?",
            (now,),
        )
        visible_message_count = conn.execute(
            """SELECT COUNT(*) AS n FROM notices
               WHERE (checked_at IS NULL AND archived_delivery = 0)
                  OR archived_delivery = 1"""
        ).fetchone()["n"]
        conn.commit()
        do_render(ticket_dir, conn)
        safe_upsert_ticket(ticket_dir)
        emit_post_write(
            conn,
            ticket_dir,
            WriteChange(
                appended_field="messages",
                added_count=1,
                new_count=int(visible_message_count),
                delivery_warning=_ARCHIVED_DELIVERY_WARNING if archived_delivery else "",
            ),
            fmt=getattr(args, "format", "plain"),
        )
        if archived_delivery and getattr(args, "format", "plain") == "plain":
            print(_ARCHIVED_DELIVERY_WARNING, file=sys.stderr)
        print(cur.lastrowid)
        return 0
    finally:
        conn.close()


def cmd_notice_checked(args: argparse.Namespace) -> int:
    ticket_dir = resolve_ticket(args.ticket)
    require_not_archived(ticket_dir, action="check messages")
    until_id = int(args.until_id)
    if until_id < 1:
        raise SystemExit("error: --until-id must be a positive integer")

    holder = identity.infer_holder(
        agent_type=getattr(args, "agent_type", ""),
        session_id=getattr(args, "session_id", ""),
        explicit_label=getattr(args, "owner_label", ""),
    )
    conn = open_db(ticket_dir)
    try:
        now = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        _require_not_archived_in_tx(conn, action="check messages")
        _require_active_holder_in_tx(conn, holder, action="check messages")
        notices = conn.execute(
            """SELECT * FROM notices
               WHERE id <= ? AND checked_at IS NULL AND archived_delivery = 0
               ORDER BY id ASC""",
            (until_id,),
        ).fetchall()
        if not notices:
            unread_count = conn.execute(
                "SELECT COUNT(*) AS n FROM notices WHERE checked_at IS NULL AND archived_delivery = 0"
            ).fetchone()["n"]
            conn.commit()
            do_render(ticket_dir, conn)
            safe_upsert_ticket(ticket_dir)
            emit_post_write(
                conn,
                ticket_dir,
                WriteChange(
                    changed_field="messages_checked",
                    old_value=f"{unread_count} unread messages",
                    new_value=f"{unread_count} unread messages",
                ),
                fmt=getattr(args, "format", "plain"),
            )
            return 0

        cv = conn.execute("SELECT work_log_md FROM current_view WHERE singleton = 1").fetchone()
        if cv is None:
            raise SystemExit("database is missing current_view row")
        work_log_md = str(cv["work_log_md"] or "")
        for notice in notices:
            work_log_md = _append_bullet(work_log_md, _notice_log_line(notice))
        work_log_md = _append_bullet(work_log_md, f"{now_hms()}: Checked messages until #{until_id}.")

        conn.execute(
            """UPDATE notices
               SET checked_at = ?, checked_by = ?, checked_by_label = ?, logged_at = ?
               WHERE id <= ? AND checked_at IS NULL AND archived_delivery = 0""",
            (now, holder.holder_id, holder.holder_label, now, until_id),
        )
        conn.execute(
            "UPDATE current_view SET work_log_md = ?, updated_at = ? WHERE singleton = 1",
            (work_log_md, now),
        )
        conn.execute(
            "UPDATE ticket_meta SET render_revision = render_revision + 1, updated_at = ?",
            (now,),
        )
        conn.commit()
        do_render(ticket_dir, conn)
        safe_upsert_ticket(ticket_dir)
        emit_post_write(
            conn,
            ticket_dir,
            WriteChange(
                appended_field="work_log",
                added_count=len(notices) + 1,
                new_count=_count_bullets(work_log_md),
            ),
            fmt=getattr(args, "format", "plain"),
        )
        return 0
    finally:
        conn.close()


cmd_message_send = cmd_notice_send
cmd_message_checked = cmd_notice_checked
