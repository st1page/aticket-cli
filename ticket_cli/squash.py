"""Squash archived small tickets into a continuing larger ticket."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import identity, ticket_new
from .atomicio import write_json_atomic, write_text_atomic
from .db import open_db
from .lifecycle import TicketInitData
from .mdparse import title_from_dir
from .paths import normalize_lifecycle_state
from .render import do_render, render_ticket
from .reporter import WriteChange, emit_post_write
from .reminders import MAX_MUST_REMEMBER_ITEMS, must_remember_items_from_raw
from .search_index import safe_delete_ticket, safe_upsert_ticket
from .timeutil import now_hms, now_iso

SQUASH_METADATA_REL = "state/squash.json"
SOURCE_SNAPSHOT_DIR_REL = "artifacts/squashed-source-snapshots"


@dataclass(frozen=True)
class SourceTicket:
    path: Path
    title: str
    created_at: str
    updated_at: str
    archived_at: str
    goal: str
    short_context: str
    must_remember: tuple[str, ...]
    items: tuple[str, ...]
    artifacts_md: str
    work_log_md: str


@dataclass(frozen=True)
class SourceAnnotationSnapshot:
    source_dir: Path
    items: str
    artifacts_md: str
    work_log_md: str
    squashed_into_ticket_dir: str
    squashed_into_ticket_uri: str
    squashed_into_at: str


def _remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def _read_text_option(value: str, file_value: str, *, label: str) -> str:
    if value and file_value:
        raise SystemExit(f"pass either --{label} or --{label}-file, not both")
    if file_value:
        return Path(file_value).expanduser().read_text(encoding="utf-8").strip()
    return str(value or "").strip()


def _append_bullet(existing: str | None, line: str) -> str:
    bullet = f"- {line}"
    body = (existing or "").rstrip()
    return bullet if not body else f"{body}\n{bullet}"


def _parse_json_items(value: str | None) -> tuple[str, ...]:
    try:
        items = json.loads(value or "[]")
    except json.JSONDecodeError:
        return ()
    if not isinstance(items, list):
        return ()
    return tuple(str(item).strip() for item in items if str(item).strip())


def _json_list_with(value: str | None, item: str) -> str:
    items = list(_parse_json_items(value))
    if item not in items:
        items.append(item)
    return json.dumps(items)


def _render_source_if_stale(source_dir: Path) -> None:
    conn = open_db(source_dir)
    try:
        meta = conn.execute("SELECT render_revision, rendered_revision FROM ticket_meta LIMIT 1").fetchone()
    finally:
        conn.close()
    if meta is None:
        raise SystemExit(f"database is missing ticket_meta row: {source_dir}")
    if meta["render_revision"] != meta["rendered_revision"]:
        render_ticket(source_dir, quiet=True)


def _load_source_ticket(raw: str) -> SourceTicket:
    source_dir = Path(raw).expanduser().resolve()
    if not source_dir.is_dir():
        raise SystemExit(f"source ticket dir not found: {source_dir}")
    if not (source_dir / "TICKET.md").is_file():
        raise SystemExit(f"source ticket missing TICKET.md: {source_dir / 'TICKET.md'}")
    _render_source_if_stale(source_dir)
    conn = open_db(source_dir)
    try:
        meta = conn.execute("SELECT * FROM ticket_meta LIMIT 1").fetchone()
        cv = conn.execute("SELECT * FROM current_view WHERE singleton = 1").fetchone()
    finally:
        conn.close()
    if meta is None or cv is None:
        raise SystemExit(f"database is missing ticket rows: {source_dir}")
    if str(meta["squashed_into_ticket_dir"] or "").strip():
        raise SystemExit(f"source ticket is already squashed: {source_dir}")
    lifecycle_state = normalize_lifecycle_state(str(meta["lifecycle_state"] or "ACTIVE"))
    if lifecycle_state != "ARCHIVED":
        raise SystemExit(f"source ticket must be ARCHIVED before squash: {source_dir} is {lifecycle_state}")
    return SourceTicket(
        path=source_dir,
        title=str(meta["title"] or title_from_dir(source_dir)).strip(),
        created_at=str(meta["created_at"] or "").strip(),
        updated_at=str(meta["updated_at"] or "").strip(),
        archived_at=str(meta["archived_at"] or "").strip(),
        goal=str(cv["goal"] or "").strip(),
        short_context=str(cv["short_context"] or "").strip(),
        must_remember=tuple(must_remember_items_from_raw(cv["must_remember"])),
        items=_parse_json_items(str(cv["items"] or "[]")),
        artifacts_md=str(cv["artifacts_md"] or "").strip(),
        work_log_md=str(cv["work_log_md"] or "").strip(),
    )


def _load_sources(raw_sources: list[str]) -> list[SourceTicket]:
    if len(raw_sources) < 2:
        raise SystemExit("tickets squash requires at least two source tickets")
    sources = [_load_source_ticket(raw) for raw in raw_sources]
    seen: set[Path] = set()
    for source in sources:
        if source.path in seen:
            raise SystemExit(f"duplicate source ticket: {source.path}")
        seen.add(source.path)
    return sorted(sources, key=lambda item: (item.created_at, item.path.name))


def _source_time_range(sources: list[SourceTicket]) -> dict[str, str]:
    starts = [source.created_at for source in sources if source.created_at]
    ends = [source.archived_at or source.updated_at for source in sources if source.archived_at or source.updated_at]
    return {
        "started_at": min(starts) if starts else "",
        "ended_at": max(ends) if ends else "",
    }


def _target_short_context(sources: list[SourceTicket], *, summary: str, next_step: str, explicit: str) -> str:
    if explicit:
        return explicit.strip()
    base = f"Squashed {len(sources)} archived tickets into this continuing ticket."
    if summary:
        base += f" Summary: {summary}"
    if next_step:
        base += f" Next: {next_step}"
    return base


def _target_artifacts(target_dir: Path, sources: list[SourceTicket]) -> str:
    lines = [
        f"- Squash metadata: {_file_uri(target_dir / SQUASH_METADATA_REL)}",
        f"- Source ticket snapshots: {_file_uri(target_dir / SOURCE_SNAPSHOT_DIR_REL)}",
    ]
    lines.extend(f"- Squashed source ticket: {_file_uri(source.path)}" for source in sources)
    for source in sources:
        for artifact in _markdown_entries(source.artifacts_md):
            lines.append(
                f"- Squashed source artifact from {_file_uri(source.path)}: "
                f"{_normalize_source_artifact_entry(source.path, artifact)}"
            )
    return "\n".join(lines)


def _target_decisions(target_dir: Path, sources: list[SourceTicket]) -> str:
    return "\n".join(
        [
            "- This ticket is a post-hoc squash of archived source tickets and is the continuing work container.",
            "- Original source tickets remain archived evidence; continue related work here instead of reopening sources.",
            f"- Source tickets contain reverse references to this ticket: {_file_uri(target_dir)}",
            f"- Squashed source count: {len(sources)}",
        ]
    )


def _target_work_log(sources: list[SourceTicket], *, target_state: str) -> str:
    lines = [f"{now_hms()}: Squashed {len(sources)} archived tickets into this {target_state} ticket."]
    lines.extend(f"{now_hms()}: Squashed source: {source.path}" for source in sources)
    for source in sources:
        source_uri = _file_uri(source.path)
        for entry in _markdown_entries(source.work_log_md):
            lines.append(f"[squashed from {source_uri}] {entry}")
    return "\n".join(f"- {line}" for line in lines)


def _target_items(sources: list[SourceTicket]) -> tuple[str, ...]:
    items: list[str] = []
    seen: set[str] = set()
    for source in sources:
        for item in (_file_uri(source.path), *source.items):
            if item not in seen:
                items.append(item)
                seen.add(item)
    return tuple(items)


def _target_must_remember(sources: list[SourceTicket]) -> tuple[str, ...]:
    items: list[str] = []
    seen: set[str] = set()
    for source in sources:
        for item in source.must_remember:
            if item not in seen:
                items.append(item)
                seen.add(item)
    if len(items) > MAX_MUST_REMEMBER_ITEMS:
        raise SystemExit(
            "error: squashed Must remember would exceed "
            f"{MAX_MUST_REMEMBER_ITEMS} entries ({len(items)}); "
            "delete entries from source tickets or squash fewer tickets first"
        )
    return tuple(items)


def _markdown_entries(markdown: str) -> tuple[str, ...]:
    entries: list[str] = []
    for raw_line in (markdown or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        if line:
            entries.append(line)
    return tuple(entries)


_RELATIVE_ARTIFACT_TOKEN = re.compile(
    r"(?<![\w:/.-])(?P<path>(?:\./)?(?:artifacts|notes|workspace|state)/[^\s)`>]+|TICKET\.md)(?![\w/.-])"
)


def _normalize_source_artifact_entry(source_dir: Path, entry: str) -> str:
    """Keep copied artifact references anchored to the original source ticket."""

    def replace(match: re.Match[str]) -> str:
        raw = match.group("path")
        trailing = ""
        while raw and raw[-1] in ".,;:!?":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        rel = raw[2:] if raw.startswith("./") else raw
        return f"{_file_uri(source_dir / rel)}{trailing}"

    return _RELATIVE_ARTIFACT_TOKEN.sub(replace, entry)


def _write_source_snapshots(target_dir: Path, sources: list[SourceTicket]) -> dict[str, str]:
    snapshot_dir = target_dir / SOURCE_SNAPSHOT_DIR_REL
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshots: dict[str, str] = {}
    for source in sources:
        snapshot_path = snapshot_dir / f"{source.path.name}.md"
        write_text_atomic(snapshot_path, (source.path / "TICKET.md").read_text(encoding="utf-8"))
        snapshots[str(source.path)] = str(snapshot_path)
    return snapshots


def _squash_metadata(
    *,
    target_dir: Path,
    sources: list[SourceTicket],
    snapshots: dict[str, str],
    squashed_at: str,
    target_state: str,
    summary: str,
    next_step: str,
) -> dict:
    return {
        "kind": "ticket-squash",
        "target_ticket_dir": str(target_dir),
        "target_lifecycle_state": target_state,
        "squashed_at": squashed_at,
        "source_time_range": _source_time_range(sources),
        "summary": summary,
        "next": next_step,
        "sources": [
            {
                "ticket_dir": str(source.path),
                "snapshot_path": snapshots.get(str(source.path), ""),
                "created_at": source.created_at,
                "archived_at": source.archived_at,
                "updated_at": source.updated_at,
                "goal": source.goal,
                "short_context": source.short_context,
            }
            for source in sources
        ],
    }


def _capture_source_annotation_snapshot(source: SourceTicket) -> SourceAnnotationSnapshot:
    conn = open_db(source.path)
    try:
        meta = conn.execute(
            "SELECT squashed_into_ticket_dir, squashed_into_ticket_uri, squashed_into_at FROM ticket_meta LIMIT 1"
        ).fetchone()
        cv = conn.execute("SELECT items, artifacts_md, work_log_md FROM current_view WHERE singleton = 1").fetchone()
    finally:
        conn.close()
    if meta is None or cv is None:
        raise SystemExit(f"database is missing ticket rows: {source.path}")
    return SourceAnnotationSnapshot(
        source_dir=source.path,
        items=str(cv["items"] or "[]"),
        artifacts_md=str(cv["artifacts_md"] or ""),
        work_log_md=str(cv["work_log_md"] or ""),
        squashed_into_ticket_dir=str(meta["squashed_into_ticket_dir"] or ""),
        squashed_into_ticket_uri=str(meta["squashed_into_ticket_uri"] or ""),
        squashed_into_at=str(meta["squashed_into_at"] or ""),
    )


def _restore_source_annotation(snapshot: SourceAnnotationSnapshot) -> None:
    conn = open_db(snapshot.source_dir)
    try:
        now = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE current_view SET items = ?, artifacts_md = ?, work_log_md = ?, updated_at = ? WHERE singleton = 1",
            (snapshot.items, snapshot.artifacts_md, snapshot.work_log_md, now),
        )
        cursor = conn.execute(
            """UPDATE ticket_meta
               SET squashed_into_ticket_dir = ?, squashed_into_ticket_uri = ?, squashed_into_at = ?,
                   render_revision = render_revision + 1, updated_at = ?
               WHERE ticket_dir = ?""",
            (
                snapshot.squashed_into_ticket_dir or None,
                snapshot.squashed_into_ticket_uri or None,
                snapshot.squashed_into_at or None,
                now,
                str(snapshot.source_dir),
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise SystemExit(
                f"restoring squash annotation updated {cursor.rowcount} ticket_meta rows, expected 1: {snapshot.source_dir}"
            )
        conn.commit()
        do_render(snapshot.source_dir, conn)
        safe_upsert_ticket(snapshot.source_dir)
    finally:
        conn.close()


def _annotate_source(source: SourceTicket, *, target_dir: Path, squashed_at: str) -> None:
    target_uri = _file_uri(target_dir)
    conn = open_db(source.path)
    try:
        now = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        meta = conn.execute("SELECT squashed_into_ticket_dir FROM ticket_meta LIMIT 1").fetchone()
        cv = conn.execute("SELECT items, artifacts_md, work_log_md FROM current_view WHERE singleton = 1").fetchone()
        if meta is None or cv is None:
            raise SystemExit(f"database is missing ticket rows: {source.path}")
        if str(meta["squashed_into_ticket_dir"] or "").strip():
            raise SystemExit(f"source ticket is already squashed: {source.path}")
        artifacts_md = _append_bullet(cv["artifacts_md"], f"Squashed into ticket: {target_uri}")
        work_log_md = _append_bullet(
            cv["work_log_md"],
            f"{now_hms()}: Squashed into ticket {target_uri}; source remains archived evidence.",
        )
        items = _json_list_with(str(cv["items"] or "[]"), target_uri)
        conn.execute(
            "UPDATE current_view SET items = ?, artifacts_md = ?, work_log_md = ?, updated_at = ? WHERE singleton = 1",
            (items, artifacts_md, work_log_md, now),
        )
        cursor = conn.execute(
            """UPDATE ticket_meta
               SET squashed_into_ticket_dir = ?, squashed_into_ticket_uri = ?, squashed_into_at = ?,
                   render_revision = render_revision + 1, updated_at = ?
               WHERE ticket_dir = ?""",
            (str(target_dir), target_uri, squashed_at, now, str(source.path)),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise SystemExit(
                f"squash annotation updated {cursor.rowcount} ticket_meta rows, expected 1: {source.path}"
            )
        conn.commit()
        do_render(source.path, conn)
        safe_upsert_ticket(source.path)
    finally:
        conn.close()


def _rollback_source_annotations(snapshots: list[SourceAnnotationSnapshot]) -> None:
    for snapshot in reversed(snapshots):
        try:
            _restore_source_annotation(snapshot)
        except Exception as exc:
            print(f"WARN: failed to roll back squash annotation for {snapshot.source_dir}: {exc}", file=sys.stderr)


def _target_init_data(
    *,
    target_dir: Path,
    holder: identity.HolderIdentity | None,
    sources: list[SourceTicket],
    target_state: str,
    squashed_at: str,
    goal: str,
    short_context: str,
    summary: str,
    next_step: str,
) -> TicketInitData:
    common = {
        "title": title_from_dir(target_dir),
        "lifecycle_state": target_state,
        "archived_at": squashed_at if target_state == "ARCHIVED" else None,
        "goal": goal,
        "short_context": short_context,
        "must_remember": _target_must_remember(sources),
        "items": _target_items(sources),
        "decisions_md": _target_decisions(target_dir, sources),
        "artifacts_md": _target_artifacts(target_dir, sources),
        "work_log_md": _target_work_log(sources, target_state=target_state),
        "env_md": "\n".join(
            part
            for part in (
                f"Squash summary: {summary}" if summary else "",
                f"Next: {next_step}" if next_step else "",
            )
            if part
        ),
    }
    if target_state == "ACTIVE":
        if holder is None:
            raise SystemExit("internal error: ACTIVE squash target missing holder")
        return ticket_new.claimed_init_data(holder, claimed_at=squashed_at, **common)
    return ticket_new.backlog_init_data(**common)


def cmd_squash(args: argparse.Namespace) -> int:
    topic = str(getattr(args, "topic", "") or "").strip()
    goal = str(getattr(args, "goal", "") or "").strip()
    if not topic:
        raise SystemExit("--topic is required")
    if not goal:
        raise SystemExit("--goal is required and cannot be empty")
    summary = _read_text_option(getattr(args, "summary", "") or "", getattr(args, "summary_file", "") or "", label="summary")
    next_step = _read_text_option(getattr(args, "next_step", "") or "", getattr(args, "next_file", "") or "", label="next")
    sources = _load_sources(list(getattr(args, "source_tickets", []) or []))

    target_state = "ACTIVE"
    if getattr(args, "backlog", False):
        target_state = "BACKLOG"
    if getattr(args, "archive", False):
        target_state = "ARCHIVED"

    has_owner_args = any(str(value or "").strip() for value in (args.agent_type, args.session_id, args.owner_label))
    if target_state != "ACTIVE" and has_owner_args:
        raise SystemExit("--backlog/--archive create an unclaimed squash target; do not pass owner identity flags")
    holder = None
    if target_state == "ACTIVE":
        holder = identity.infer_holder(
            agent_type=getattr(args, "agent_type", ""),
            session_id=getattr(args, "session_id", ""),
            explicit_label=getattr(args, "owner_label", ""),
        )

    target_dir: Path | None = None
    try:
        target_dir = ticket_new.scaffold_ticket(topic)
        squashed_at = now_iso()
        snapshots = _write_source_snapshots(target_dir, sources)
        metadata = _squash_metadata(
            target_dir=target_dir,
            sources=sources,
            snapshots=snapshots,
            squashed_at=squashed_at,
            target_state=target_state,
            summary=summary,
            next_step=next_step,
        )
        write_json_atomic(target_dir / SQUASH_METADATA_REL, metadata)
        short_context = _target_short_context(
            sources,
            summary=summary,
            next_step=next_step,
            explicit=getattr(args, "short_context", "") or "",
        )
        ticket_new.initialize_new_ticket(
            target_dir,
            _target_init_data(
                target_dir=target_dir,
                holder=holder,
                sources=sources,
                target_state=target_state,
                squashed_at=squashed_at,
                goal=goal,
                short_context=short_context,
                summary=summary,
                next_step=next_step,
            ),
            render=True,
            quiet=True,
        )
    except BaseException:
        if target_dir is not None:
            safe_delete_ticket(target_dir)
            _remove_tree(target_dir)
        raise

    annotation_snapshots: list[SourceAnnotationSnapshot] = []
    try:
        for source in sources:
            snapshot = _capture_source_annotation_snapshot(source)
            annotation_snapshots.append(snapshot)
            _annotate_source(source, target_dir=target_dir, squashed_at=squashed_at)
    except BaseException:
        _rollback_source_annotations(annotation_snapshots)
        if target_dir is not None:
            safe_delete_ticket(target_dir)
            _remove_tree(target_dir)
        raise

    print(str(target_dir))
    conn = open_db(target_dir)
    try:
        emit_post_write(
            conn,
            target_dir,
            WriteChange(changed_field="lifecycle", old_value="", new_value=target_state),
            fmt=getattr(args, "format", "plain"),
        )
    finally:
        conn.close()
    return 0
