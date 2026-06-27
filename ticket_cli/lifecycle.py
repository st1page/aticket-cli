"""Ticket lifecycle + current-view mutations."""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .config import archive_large_dir_thresholds_bytes
from .db import SCHEMA_VERSION, db_path, ensure_schema, open_db, open_sqlite
from .mdparse import dump_fork_metadata
from .paths import normalize_lifecycle_state
from .render import do_render
from .reporter import WriteChange, emit_post_write
from .reminders import MAX_MUST_REMEMBER_ITEMS
from .search_index import safe_upsert_ticket
from .timeutil import now_hms, now_iso

_VALID_CV_FIELDS = frozenset({
    "goal", "short_context", "must_remember", "scope_non_goals", "items", "links_extra_md",
    "decisions_md", "artifacts_md", "work_log_md", "env_md",
})

@dataclass(frozen=True)
class TicketInitData:
    title: str = ""
    goal: str = ""
    short_context: str = ""
    must_remember: tuple[str, ...] = ()
    scope_non_goals: str = ""
    items: tuple[str, ...] = ()
    links_extra_md: str = ""
    decisions_md: str = ""
    artifacts_md: str = ""
    work_log_md: str = ""
    env_md: str = ""
    source_ticket_dir: str | None = None
    canonical_source: str | None = None
    fork_metadata: dict | None = None
    lifecycle_state: str = "ACTIVE"
    archived_at: str | None = None
    owner_id: str | None = None
    owner_label: str | None = None
    owner_claimed_at: str | None = None
    owner_released_at: str | None = None
    owner_last_action: str | None = None
    owner_last_actor_id: str | None = None
    owner_last_actor_label: str | None = None
    owner_last_action_at: str | None = None


# ── helpers ───────────────────────────────────────────────────────────


def resolve_ticket(raw: str) -> Path:
    p = Path(raw).expanduser().resolve()
    if not p.is_dir():
        raise SystemExit(f"ticket dir not found: {p}")
    return p


def require_not_archived(ticket_dir: Path, *, action: str) -> None:
    conn = open_db(ticket_dir)
    try:
        meta = conn.execute("SELECT lifecycle_state FROM ticket_meta LIMIT 1").fetchone()
    finally:
        conn.close()
    if meta is not None and normalize_lifecycle_state(str(meta["lifecycle_state"] or "ACTIVE")) == "ARCHIVED":
        raise SystemExit(
            f"refuse to {action}: ticket is ARCHIVED and cannot be modified; "
            "fork it or create a new ticket instead"
        )


def _format_mib(size_bytes: int) -> str:
    return f"{size_bytes / 1024 / 1024:.2f} MiB"


def _ticket_dir_size_bytes(ticket_dir: Path) -> int:
    """Return apparent size in bytes without following symlinks."""
    total = 0
    stack = [ticket_dir]
    while stack:
        current = stack.pop()
        try:
            stat = current.stat(follow_symlinks=False)
        except OSError as exc:
            raise SystemExit(f"cannot measure ticket directory size: {current}: {exc}") from exc
        total += int(stat.st_size)
        if not current.is_dir() or current.is_symlink():
            continue
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    stack.append(Path(entry.path))
        except OSError as exc:
            raise SystemExit(f"cannot measure ticket directory size: {current}: {exc}") from exc
    return total


def _require_large_archive_confirmation(args: argparse.Namespace, ticket_dir: Path) -> None:
    if not db_path(ticket_dir).is_file():
        return
    size_bytes = _ticket_dir_size_bytes(ticket_dir)
    agent_threshold_bytes, human_threshold_bytes = archive_large_dir_thresholds_bytes()
    if size_bytes > human_threshold_bytes:
        if not getattr(args, "human_confirm_archive_large_dir", False):
            raise SystemExit(
                "refuse to archive: ticket directory exceeds the human-confirm archive threshold "
                f"({_format_mib(size_bytes)} at {ticket_dir}). Before archiving, inspect artifacts/workspace, "
                "compress or move raw/detail data that does not need to stay expanded, and record the final "
                "result or handoff state in the ticket. Ask a human to approve keeping this large ticket, then "
                "rerun with --human-confirm-archive-large-dir."
            )
        return
    if size_bytes > agent_threshold_bytes:
        if not (
            getattr(args, "agent_confirm_archive_large_dir", False)
            or getattr(args, "human_confirm_archive_large_dir", False)
        ):
            raise SystemExit(
                "refuse to archive: ticket directory exceeds the agent-confirm archive threshold "
                f"({_format_mib(size_bytes)} at {ticket_dir}). Before archiving, inspect artifacts/workspace, "
                "compress raw/detail data when practical, and make sure the ticket records the final result "
                "or next owner. Rerun with --agent-confirm-archive-large-dir after confirming this large "
                "ticket should be archived as-is."
            )


def _check_field(field: str) -> None:
    if field not in _VALID_CV_FIELDS:
        raise ValueError(f"invalid current_view field: {field}")


def _append_bullet_text(existing: str | None, line: str) -> str:
    bullet = f"- {line}"
    body = (existing or "").rstrip()
    return bullet if not body else f"{body}\n{bullet}"


def initialize_ticket_db(ticket_dir: Path, init_data: TicketInitData) -> Path:
    dbp = db_path(ticket_dir)
    if dbp.exists():
        raise SystemExit(f"sqlite db already exists: {dbp}")
    dbp.parent.mkdir(parents=True, exist_ok=True)
    conn = open_sqlite(dbp)
    try:
        ensure_schema(conn)
        now = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO ticket_meta
               (ticket_dir, title, created_at, updated_at,
                source_ticket_dir, canonical_source, fork_metadata_json,
                lifecycle_state, archived_at,
                owner_id, owner_label, owner_claimed_at, owner_released_at,
                owner_last_action, owner_last_actor_id, owner_last_actor_label,
                owner_last_action_at,
                squashed_into_ticket_dir, squashed_into_ticket_uri, squashed_into_at,
                render_revision, rendered_revision, schema_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 1, 0, ?)""",
            (
                str(ticket_dir), init_data.title, now, now,
                init_data.source_ticket_dir, init_data.canonical_source,
                dump_fork_metadata(init_data.fork_metadata),
                init_data.lifecycle_state, init_data.archived_at,
                init_data.owner_id, init_data.owner_label, init_data.owner_claimed_at,
                init_data.owner_released_at, init_data.owner_last_action,
                init_data.owner_last_actor_id, init_data.owner_last_actor_label,
                init_data.owner_last_action_at, SCHEMA_VERSION,
            ),
        )
        conn.execute(
            """INSERT INTO current_view
               (singleton, goal, short_context, must_remember,
                scope_non_goals, items,
                links_extra_md, decisions_md, artifacts_md,
                work_log_md, env_md, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                init_data.goal, init_data.short_context,
                json.dumps(list(init_data.must_remember), ensure_ascii=False),
                init_data.scope_non_goals,
                json.dumps(list(init_data.items)),
                init_data.links_extra_md, init_data.decisions_md,
                init_data.artifacts_md, init_data.work_log_md,
                init_data.env_md, now,
            ),
        )
        conn.commit()
        return dbp
    finally:
        conn.close()


def _update_field(ticket_dir: Path, field: str, value: str, *, no_render: bool, change: WriteChange | None = None, fmt: str = "plain") -> int:
    """Replace `current_view.<field>`. If `change` is given, emit a post-write
    snapshot after the commit. To keep the snapshot's `old → new` diff
    accurate even under concurrent writers, the old value is captured inside
    the same BEGIN IMMEDIATE transaction as the write (any value present on
    `change.old_value` from the caller is overridden)."""
    _check_field(field)
    require_not_archived(ticket_dir, action=f"update {field}")
    conn = open_db(ticket_dir)
    try:
        now = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        # Capture old value inside the same exclusive transaction as the write
        # so the reporter's `old → new` line cannot reflect a stale read.
        if change is not None:
            row = conn.execute(f"SELECT {field} FROM current_view WHERE singleton = 1").fetchone()
            captured_old = str(row[field] or "") if row else ""
            change = WriteChange(
                changed_field=change.changed_field,
                old_value=captured_old,
                new_value=change.new_value,
                appended_field=change.appended_field,
                added_count=change.added_count,
                new_count=change.new_count,
                was_already_present=change.was_already_present,
            )
        conn.execute(f"UPDATE current_view SET {field} = ?, updated_at = ? WHERE singleton = 1", (value, now))
        conn.execute("UPDATE ticket_meta SET render_revision = render_revision + 1, updated_at = ?", (now,))
        conn.commit()
        if not no_render:
            do_render(ticket_dir, conn)
        safe_upsert_ticket(ticket_dir)
        if change is not None:
            emit_post_write(conn, ticket_dir, change, fmt=fmt)
        return 0
    finally:
        conn.close()


def _read_field(ticket_dir: Path, field: str) -> str:
    """Read a single current_view field.

    Now mainly useful for callers that need a single field out-of-transaction.
    The reporter's diff capture is done inside `_update_field` itself, so
    callers that just want the snapshot's old→new diff no longer need this."""
    _check_field(field)
    conn = open_db(ticket_dir)
    try:
        row = conn.execute(f"SELECT {field} FROM current_view WHERE singleton = 1").fetchone()
        if row is None:
            return ""
        return str(row[field] or "")
    finally:
        conn.close()


def _append_field(ticket_dir: Path, field: str, lines: list[str], *, no_render: bool, change_field_label: str | None = None, fmt: str = "plain") -> int:
    """Append one or more `- <line>` bullets to `current_view.<field>` atomically.

    Each item in `lines` becomes its own bullet. The whole batch lands in a
    single transaction so a `log` call with N lines is N append-only writes
    that either all succeed together or all roll back.

    If `change_field_label` is given, emit a post-write snapshot tagged with it
    (with `added_count = len(lines)` and the recomputed total bullet count)."""
    _check_field(field)
    if not lines:
        return 0
    require_not_archived(ticket_dir, action=f"append {change_field_label or field}")
    conn = open_db(ticket_dir)
    try:
        now = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        for line in lines:
            bullet = f"- {line}"
            # Atomic append via SQL concat to avoid read-modify-write races.
            conn.execute(
                f"""UPDATE current_view SET
                    {field} = CASE
                        WHEN {field} IS NULL OR {field} = '' THEN ?
                        WHEN substr({field}, -1) = char(10) THEN {field} || ?
                        ELSE {field} || char(10) || ?
                    END,
                    updated_at = ?
                WHERE singleton = 1""",
                (bullet, bullet, bullet, now),
            )
        conn.execute("UPDATE ticket_meta SET render_revision = render_revision + 1, updated_at = ?", (now,))
        conn.commit()
        if not no_render:
            do_render(ticket_dir, conn)
        safe_upsert_ticket(ticket_dir)
        if change_field_label is not None:
            # Recount bullets post-write so the displayed total matches reality.
            updated = conn.execute(f"SELECT {field} FROM current_view WHERE singleton = 1").fetchone()
            body = str(updated[field] or "") if updated else ""
            count = sum(1 for ln in body.splitlines() if ln.lstrip().startswith("- "))
            emit_post_write(
                conn, ticket_dir,
                WriteChange(
                    appended_field=change_field_label,
                    added_count=len(lines),
                    new_count=count,
                ),
                fmt=fmt,
            )
        return 0
    finally:
        conn.close()


def _append_json_list_field(ticket_dir: Path, field: str, value: str, *, no_render: bool, change_field_label: str | None = None, fmt: str = "plain") -> int:
    """Append a single value to a JSON-encoded list field if not already
    present. If `change_field_label` is given, emit a post-write snapshot with
    add-item semantics (count + was_already_present)."""
    _check_field(field)
    require_not_archived(ticket_dir, action=f"append {change_field_label or field}")
    conn = open_db(ticket_dir)
    try:
        now = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(f"SELECT {field} FROM current_view WHERE singleton = 1").fetchone()
        val_raw = row[field] if row else None
        vals = json.loads(val_raw) if val_raw else []
        if not isinstance(vals, list):
            vals = [val_raw] if val_raw else []
        was_already_present = value in vals
        if not was_already_present:
            vals.append(value)
        conn.execute(f"UPDATE current_view SET {field} = ?, updated_at = ? WHERE singleton = 1", (json.dumps(vals), now))
        conn.execute("UPDATE ticket_meta SET render_revision = render_revision + 1, updated_at = ?", (now,))
        conn.commit()
        if not no_render:
            do_render(ticket_dir, conn)
        safe_upsert_ticket(ticket_dir)
        if change_field_label is not None:
            emit_post_write(
                conn, ticket_dir,
                WriteChange(
                    appended_field=change_field_label,
                    added_count=0 if was_already_present else 1,
                    new_count=len(vals),
                    was_already_present=was_already_present,
                ),
                fmt=fmt,
            )
        return 0
    finally:
        conn.close()


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [str(raw)] if str(raw or "").strip() else []
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [str(parsed)] if parsed else []


def _update_json_list(
    ticket_dir: Path,
    field: str,
    updater,
    *,
    action: str,
    fmt: str = "plain",
    no_render: bool = False,
) -> int:
    _check_field(field)
    require_not_archived(ticket_dir, action=action)
    conn = open_db(ticket_dir)
    try:
        now = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(f"SELECT {field} FROM current_view WHERE singleton = 1").fetchone()
        current = _parse_json_list(row[field] if row else "")
        updated, change = updater(list(current))
        conn.execute(
            f"UPDATE current_view SET {field} = ?, updated_at = ? WHERE singleton = 1",
            (json.dumps(updated, ensure_ascii=False), now),
        )
        conn.execute("UPDATE ticket_meta SET render_revision = render_revision + 1, updated_at = ?", (now,))
        conn.commit()
        if not no_render:
            do_render(ticket_dir, conn)
        safe_upsert_ticket(ticket_dir)
        emit_post_write(conn, ticket_dir, change, fmt=fmt)
        return 0
    finally:
        conn.close()


# ── command handlers (called from cli.py via set_defaults(func=...)) ───


def _escape_inline_breaks(raw: str) -> str:
    """Keep one CLI payload as one rendered markdown line."""
    text = str(raw or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\n", "\\n")


def _read_text_arg(args: argparse.Namespace, *, command: str) -> str:
    usage_command = f"ticket <ticket-dir> {command}"
    if getattr(args, "file", ""):
        text = Path(args.file).read_text(encoding="utf-8").strip()
    else:
        text = _escape_inline_breaks(str(getattr(args, "payload", "") or "")).strip()
    if not text:
        raise SystemExit(f"error: {usage_command} requires non-empty positional text or --file")
    return text


def cmd_change_goal(args: argparse.Namespace) -> int:
    ticket_dir = resolve_ticket(args.ticket)
    text = _read_text_arg(args, command="goal")
    return _update_field(
        ticket_dir, "goal", text, no_render=args.no_render,
        change=WriteChange(changed_field="goal", new_value=text),
        fmt=getattr(args, "format", "plain"),
    )


def cmd_short_context(args: argparse.Namespace) -> int:
    ticket_dir = resolve_ticket(args.ticket)
    text = _read_text_arg(args, command="context")
    return _update_field(
        ticket_dir, "short_context", text, no_render=args.no_render,
        change=WriteChange(changed_field="short_context", new_value=text),
        fmt=getattr(args, "format", "plain"),
    )


def _collect_append_lines(args: argparse.Namespace, *, allow_file: bool = True) -> list[str]:
    """Pull append payload from positional arguments and `--file`.

    Order: all positional values first (in CLI order), then file contents.
    Embedded newlines inside one positional value are escaped so the rendered
    ticket keeps one CLI payload as one markdown bullet. File contents still
    append one non-empty file line per bullet. Returns [] when nothing was given
    — handler decides whether that is an error."""
    raw_lines = getattr(args, "payload", None) or []
    if isinstance(raw_lines, str):
        raw_lines = [raw_lines]
    out: list[str] = []
    for entry in raw_lines:
        stripped = _escape_inline_breaks(str(entry)).strip()
        if stripped:
            if stripped.startswith("--"):
                raise SystemExit("error: append payload must not start with option syntax")
            out.append(stripped)
    file_path = getattr(args, "file", "") if allow_file else ""
    if file_path:
        body = Path(file_path).read_text(encoding="utf-8")
        for ln in body.splitlines():
            stripped = ln.rstrip()
            if stripped.strip():
                out.append(stripped)
    return out


def cmd_append_must_remember(args: argparse.Namespace) -> int:
    lines = _collect_append_lines(args)
    if not lines:
        raise SystemExit("error: ticket <ticket-dir> remember requires at least one positional line or --file")
    ticket_dir = resolve_ticket(args.ticket)

    def _append(current: list[str]) -> tuple[list[str], WriteChange]:
        if len(current) + len(lines) > MAX_MUST_REMEMBER_ITEMS:
            raise SystemExit(
                "error: Must remember is full "
                f"({len(current)}/{MAX_MUST_REMEMBER_ITEMS}); delete an entry first with "
                "`aticket-cli ticket <ticket-dir> forget <index>`"
            )
        updated = current + lines
        return updated, WriteChange(
            appended_field="must_remember",
            added_count=len(lines),
            new_count=len(updated),
        )

    return _update_json_list(
        ticket_dir, "must_remember", _append,
        action="append must_remember",
        fmt=getattr(args, "format", "plain"),
        no_render=args.no_render,
    )


def cmd_forget_must_remember(args: argparse.Namespace) -> int:
    raw_index = str(getattr(args, "index", "") or "").strip()
    if not raw_index:
        raise SystemExit("error: ticket <ticket-dir> forget requires a 1-based INDEX")
    try:
        index = int(raw_index)
    except ValueError as exc:
        raise SystemExit("error: forget INDEX must be an integer") from exc
    if index < 1:
        raise SystemExit("error: forget INDEX is 1-based and must be >= 1")
    ticket_dir = resolve_ticket(args.ticket)

    def _remove(current: list[str]) -> tuple[list[str], WriteChange]:
        if index > len(current):
            raise SystemExit(f"error: forget INDEX out of range: {index} (must be 1..{len(current)})")
        removed = current[index - 1]
        updated = current[: index - 1] + current[index:]
        return updated, WriteChange(
            changed_field="must_remember",
            old_value=removed,
            new_value=f"{len(updated)} entries",
        )

    return _update_json_list(
        ticket_dir, "must_remember", _remove,
        action="delete must_remember",
        fmt=getattr(args, "format", "plain"),
        no_render=args.no_render,
    )


def cmd_append_work_log(args: argparse.Namespace) -> int:
    raw_lines = _collect_append_lines(args)
    if not raw_lines:
        raise SystemExit("error: ticket <ticket-dir> log requires at least one positional line or --file")
    # Each log line gets its own timestamp prefix.
    stamped = [f"{now_hms()}: {ln}" for ln in raw_lines]
    return _append_field(
        resolve_ticket(args.ticket), "work_log_md", stamped,
        no_render=args.no_render,
        change_field_label="work_log",
        fmt=getattr(args, "format", "plain"),
    )


def cmd_append_item(args: argparse.Namespace) -> int:
    uri = str(getattr(args, "uri", "") or "").strip()
    if not uri:
        raise SystemExit("error: ticket <ticket-dir> add-item requires a non-empty positional URI")
    if "\n" in uri or "\r" in uri:
        raise SystemExit("error: add-item URI must be a single-line value")
    return _append_json_list_field(
        resolve_ticket(args.ticket), "items", uri,
        no_render=args.no_render,
        change_field_label="items",
        fmt=getattr(args, "format", "plain"),
    )


def update_lifecycle_state(args: argparse.Namespace) -> int:
    ticket_dir = resolve_ticket(args.ticket)
    fmt = getattr(args, "format", "plain")
    lifecycle_state = normalize_lifecycle_state(args.lifecycle_state)
    conn = open_db(ticket_dir)
    try:
        now = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        meta = conn.execute("SELECT * FROM ticket_meta LIMIT 1").fetchone()
        cv = conn.execute("SELECT * FROM current_view WHERE singleton = 1").fetchone()
        if meta is None or cv is None:
            raise SystemExit("database is missing ticket_meta or current_view row")
        prev_state = normalize_lifecycle_state(str(meta["lifecycle_state"] or "ACTIVE"))
        prev_archived_at = str(meta["archived_at"] or "").strip()
        next_archived_at = now if lifecycle_state == "ARCHIVED" else None
        if lifecycle_state == "ARCHIVED" and prev_state != "ARCHIVED":
            _require_large_archive_confirmation(args, ticket_dir)
        if lifecycle_state == "ARCHIVED":
            unread_count = conn.execute(
                "SELECT COUNT(*) AS n FROM notices WHERE checked_at IS NULL AND archived_delivery = 0"
            ).fetchone()["n"]
            if int(unread_count) > 0:
                noun = "message" if int(unread_count) == 1 else "messages"
                raise SystemExit(
                    f"refuse to archive: ticket has {int(unread_count)} unread {noun}; "
                    f"read {ticket_dir / 'TICKET.md'} section '## Messages' and run "
                    f"aticket-cli ticket \"{ticket_dir}\" message checked --until-id <message-id> first"
                )
        if prev_state == lifecycle_state:
            if lifecycle_state == "ARCHIVED" and not prev_archived_at:
                conn.execute(
                    "UPDATE ticket_meta SET archived_at = ?, render_revision = render_revision + 1, updated_at = ? WHERE ticket_dir = ?",
                    (next_archived_at, now, str(ticket_dir)),
                )
            conn.commit()
            if not args.no_render:
                do_render(ticket_dir, conn)
            safe_upsert_ticket(ticket_dir)
            # No-op archive/update: show the snapshot but no diff (old==new).
            emit_post_write(
                conn, ticket_dir,
                WriteChange(changed_field="lifecycle", old_value=prev_state, new_value=lifecycle_state),
                fmt=fmt,
            )
            return 0
        work_log_md = _append_bullet_text(
            cv["work_log_md"], f"{now_hms()}: Lifecycle changed from `{prev_state}` to `{lifecycle_state}`."
        )
        conn.execute(
            "UPDATE ticket_meta SET lifecycle_state = ?, archived_at = ?, render_revision = render_revision + 1, updated_at = ? WHERE ticket_dir = ?",
            (lifecycle_state, next_archived_at, now, str(ticket_dir)),
        )
        conn.execute("UPDATE current_view SET work_log_md = ?, updated_at = ? WHERE singleton = 1", (work_log_md, now))
        conn.commit()
        if not args.no_render:
            do_render(ticket_dir, conn)
        safe_upsert_ticket(ticket_dir)
        emit_post_write(
            conn, ticket_dir,
            WriteChange(changed_field="lifecycle", old_value=prev_state, new_value=lifecycle_state),
            fmt=fmt,
        )
        return 0
    finally:
        conn.close()


def cmd_archive(args: argparse.Namespace) -> int:
    args.lifecycle_state = "ARCHIVED"
    args.no_render = False
    return update_lifecycle_state(args)
