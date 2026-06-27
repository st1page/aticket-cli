"""Render TICKET.md from the sqlite truth source.

The freshness protocol captures target_revision under BEGIN IMMEDIATE before
writing rendered_revision, so a concurrent writer's bump is not mistaken for
"already rendered".
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .atomicio import write_text_atomic
from .db import open_db
from .mdparse import format_fork_section, title_from_dir
from .paths import MANAGED_MARKER
from .timeutil import now_iso


def _notice_metadata(notice: sqlite3.Row) -> list[str]:
    metadata: list[str] = []
    from_ticket = str(notice["from_ticket"] or "").strip()
    if from_ticket:
        metadata.append(f"from={from_ticket}")
    raw_with_items = str(notice["with_items_json"] or "[]")
    try:
        with_items = [str(item).strip() for item in json.loads(raw_with_items) if str(item).strip()]
    except json.JSONDecodeError:
        with_items = []
    metadata.extend(f"with={item}" for item in with_items)
    sender = str(notice["sender"] or "").strip()
    if sender:
        metadata.append(f"by={sender}")
    return metadata


def _render_message_line(message: sqlite3.Row) -> str:
    metadata = " ".join(_notice_metadata(message))
    suffix = f" {metadata}" if metadata else ""
    return f"- [message #{message['id']}] {message['created_at']} {message['message']}{suffix}"


def _render_messages(messages: list[sqlite3.Row]) -> str:
    unread = [row for row in messages if not int(row["archived_delivery"] or 0)]
    archived = [row for row in messages if int(row["archived_delivery"] or 0)]
    if not unread and not archived:
        return ""

    lines: list[str] = []
    if unread:
        lines.extend([f"Unread: {len(unread)}", ""])
        lines.extend(_render_message_line(message) for message in unread)
    if archived:
        if lines:
            lines.append("")
        lines.extend(["Archived / no delivery guarantee:", ""])
        lines.extend(_render_message_line(message) for message in archived)
    return "\n".join(lines).rstrip()


def _render_ticket_md(ticket_dir: Path, meta: sqlite3.Row, cv: sqlite3.Row, messages: list[sqlite3.Row]) -> None:
    title = meta["title"] or title_from_dir(ticket_dir)

    def _section(name: str, body: str | None) -> str:
        b = (body or "").strip()
        if b:
            return f"## {name}\n{b}\n"
        return f"## {name}\n\n"

    def _ordered_json_list_section(name: str, raw: str | None) -> str:
        if not raw:
            return f"## {name}\n\n"
        try:
            values = json.loads(raw)
        except json.JSONDecodeError:
            values = [raw]
        if not isinstance(values, list):
            values = [values]
        body = "\n".join(
            f"{idx}. {str(value).strip()}"
            for idx, value in enumerate(values, start=1)
            if str(value).strip()
        )
        return f"## {name}\n{body}\n" if body else f"## {name}\n\n"

    items_raw = cv["items"]
    items = json.loads(items_raw) if items_raw else []
    links_lines = [f"- {uri}" for uri in items if str(uri).strip()]
    if cv["links_extra_md"]:
        links_lines.extend(line for line in cv["links_extra_md"].splitlines() if line.strip())
    links_body = "\n".join(links_lines)
    fork_body = format_fork_section(meta["fork_metadata_json"])

    lifecycle_line = f"Lifecycle: {meta['lifecycle_state'] or 'ACTIVE'}"
    archived_at_line = f"Archived at: {meta['archived_at']}" if meta["archived_at"] else ""
    owner_line = ""
    owner_action = str(meta["owner_last_action"] or "").strip()
    owner_actor = str(meta["owner_last_actor_label"] or "").strip()
    owner_at = str(meta["owner_last_action_at"] or "").strip()
    if owner_action and owner_actor and owner_at:
        owner_line = f"Owner: {owner_action} by {owner_actor} at {owner_at}"
    squashed_into_uri = str(meta["squashed_into_ticket_uri"] or "").strip()
    squashed_into_line = f"Squashed into: {squashed_into_uri}" if squashed_into_uri else ""

    parts = [
        MANAGED_MARKER,
        title,
        "",
        lifecycle_line,
        archived_at_line,
        owner_line,
        squashed_into_line,
        "",
        _section("Goal", cv["goal"]),
        _section("Short context", cv["short_context"]),
        _ordered_json_list_section("Must remember", cv["must_remember"]),
        _section("Messages", _render_messages(messages)),
        _section("Scope / Non-goals", cv["scope_non_goals"]),
        _section("Fork", fork_body) if fork_body else "",
        f"## Items\n{links_body}\n",
        _section("Environment", cv["env_md"]),
        _section("Work log", cv["work_log_md"]),
        _section("Decisions", cv["decisions_md"]),
        _section("Artifacts", cv["artifacts_md"]),
        "---",
        f"Rendered from DB revision: {meta['render_revision']}",
        f"Rendered at: {now_iso()}",
        "",
    ]
    write_text_atomic(ticket_dir / "TICKET.md", "\n".join(p for p in parts if p != ""))


def do_render(ticket_dir: Path, conn: sqlite3.Connection, *, verbose: bool = False) -> None:
    """Re-render TICKET.md from sqlite. `verbose` is preserved for callers but
    no longer prints `rendered (revision=N)` — write handlers now emit a richer
    post-write snapshot via ticket_cli.reporter."""
    conn.execute("BEGIN IMMEDIATE")
    meta = conn.execute("SELECT * FROM ticket_meta LIMIT 1").fetchone()
    cv = conn.execute("SELECT * FROM current_view WHERE singleton = 1").fetchone()
    messages = conn.execute(
        """SELECT * FROM notices
           WHERE (checked_at IS NULL AND archived_delivery = 0)
              OR archived_delivery = 1
           ORDER BY id ASC"""
    ).fetchall()
    if meta is None or cv is None:
        conn.rollback()
        raise SystemExit("database is missing ticket_meta or current_view row")

    target_revision = meta["render_revision"]
    _render_ticket_md(ticket_dir, meta, cv, messages)
    now = now_iso()
    conn.execute(
        "UPDATE ticket_meta SET rendered_revision = ?, last_rendered_at = ?",
        (target_revision, now),
    )
    conn.commit()


def render_ticket(ticket_dir: Path, *, quiet: bool = True) -> None:
    """Re-render TICKET.md from sqlite. Rendering is quiet by default."""
    conn = open_db(ticket_dir)
    try:
        do_render(ticket_dir, conn)
    finally:
        conn.close()
