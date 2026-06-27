"""`aticket-cli ticket new` / `aticket-cli ticket <dir> claim|release`.

Ports new_session.sh into Python. Greenfield differences:
  * init writes the sqlite DB directly from a blank template (title derived from
    the dir name); there is no SESSION.md-prose → parse → render round-trip.
  * claim/release state lives in sqlite ticket_meta, not sidecar files.
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

from . import identity
from .db import open_db
from .lifecycle import TicketInitData, initialize_ticket_db, require_not_archived
from .mdparse import title_from_dir
from .paths import agent_tickets_root, canonical_tickets_dir
from .render import do_render, render_ticket
from .reporter import WriteChange, emit_post_write
from .search_index import safe_upsert_ticket
from .timeutil import now_hms, now_iso

SUBDIRS = ("notes", "artifacts", "workspace", "state")


def _slugify(topic: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    return slug or "ticket"


def _work_log_line(action: str, holder: identity.HolderIdentity, previous_label: str = "") -> str:
    if action == "claim":
        suffix = f" Previous holder: `{previous_label}`." if previous_label else ""
        return f"{now_hms()}: Claimed ticket by `{holder.holder_label}`.{suffix}"
    return f"{now_hms()}: Released ticket by `{holder.holder_label}`."


def scaffold_ticket(topic: str) -> Path:
    """Create the ticket directory, WITHOUT initializing sqlite.

    Used both by `create_ticket` (which then inits the DB) and by `fork` (which
    inits the DB via the fork path)."""
    date = datetime.now().strftime("%F")
    tm = datetime.now().strftime("%H%M%S")
    ticket_dir = canonical_tickets_dir(agent_tickets_root()) / f"{date}-{_slugify(topic)}-{tm}"
    for sub in SUBDIRS:
        (ticket_dir / sub).mkdir(parents=True, exist_ok=True)
    gitignore = ticket_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("workspace/\nstate/\n", encoding="utf-8")
    return ticket_dir


def claimed_init_data(
    holder: identity.HolderIdentity,
    *,
    claimed_at: str = "",
    **fields,
) -> TicketInitData:
    """Build init data for a ticket that starts claimed by `holder`.

    `aticket-cli ticket new` and `aticket-cli ticket <dir> fork` both create a new independent ticket lease.
    Fork only adds source metadata/snapshot fields on top of this shared new
    ticket contract.
    """
    now = claimed_at or now_iso()
    return TicketInitData(
        owner_id=holder.holder_id,
        owner_label=holder.holder_label,
        owner_claimed_at=now,
        owner_last_action="claim",
        owner_last_actor_id=holder.holder_id,
        owner_last_actor_label=holder.holder_label,
        owner_last_action_at=now,
        **fields,
    )


def backlog_init_data(**fields) -> TicketInitData:
    """Build init data for a ticket that starts unclaimed and not yet started."""
    return TicketInitData(
        owner_id=None,
        owner_label=None,
        owner_claimed_at=None,
        owner_released_at=None,
        owner_last_action=None,
        owner_last_actor_id=None,
        owner_last_actor_label=None,
        owner_last_action_at=None,
        **fields,
    )


def initialize_new_ticket(ticket_dir: Path, init_data: TicketInitData, *, render: bool = True, quiet: bool = True) -> Path:
    initialize_ticket_db(ticket_dir, init_data)
    if render:
        render_ticket(ticket_dir, quiet=quiet)
    safe_upsert_ticket(ticket_dir)
    return ticket_dir


def create_ticket(
    topic: str,
    *,
    goal: str,
    short_context: str = "",
    agent_type: str = "",
    session_id: str = "",
    holder_label: str = "",
    backlog: bool = False,
    render: bool = True,
    quiet: bool = True,
) -> Path:
    goal = (goal or "").strip()
    if not goal:
        raise SystemExit("--goal is required and cannot be empty")
    if backlog:
        if any(str(value or "").strip() for value in (agent_type, session_id, holder_label)):
            raise SystemExit("--backlog creates an unclaimed ticket; do not pass owner identity flags")
        ticket_dir = scaffold_ticket(topic)
        return initialize_new_ticket(
            ticket_dir,
            backlog_init_data(
                title=title_from_dir(ticket_dir),
                lifecycle_state="BACKLOG",
                goal=goal,
                short_context=short_context.strip(),
                work_log_md="",
            ),
            render=render,
            quiet=quiet,
        )
    holder = identity.infer_holder(agent_type=agent_type, session_id=session_id, explicit_label=holder_label)
    ticket_dir = scaffold_ticket(topic)
    return initialize_new_ticket(
        ticket_dir,
        claimed_init_data(
            holder,
            title=title_from_dir(ticket_dir),
            lifecycle_state="ACTIVE",
            goal=goal,
            short_context=short_context.strip(),
            work_log_md=f"- {_work_log_line('claim', holder)}",
        ),
        render=render,
        quiet=quiet,
    )


def claim_ticket(
    ticket_path: str,
    *,
    agent_type: str = "",
    session_id: str = "",
    holder_label: str = "",
    force: bool = False,
    confirm_human_approved_takeover: bool = False,
    render: bool = True,
    quiet: bool = True,
) -> Path:
    ticket_dir = Path(ticket_path).expanduser().resolve()
    if not ticket_dir.is_dir():
        raise SystemExit(f"ticket directory does not exist: {ticket_dir}")
    if not (ticket_dir / "TICKET.md").is_file():
        raise SystemExit(f"not a valid ticket directory (no TICKET.md): {ticket_dir}")
    require_not_archived(ticket_dir, action="claim ticket")

    holder = identity.infer_holder(agent_type=agent_type, session_id=session_id, explicit_label=holder_label)
    conn = open_db(ticket_dir)
    try:
        now = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        meta = conn.execute("SELECT * FROM ticket_meta LIMIT 1").fetchone()
        cv = conn.execute("SELECT * FROM current_view WHERE singleton = 1").fetchone()
        if meta is None or cv is None:
            raise SystemExit("database is missing ticket_meta or current_view row")
        previous_id = str(meta["owner_id"] or "").strip()
        previous_label = str(meta["owner_label"] or "").strip()
        taking_over_active_holder = bool(previous_id and previous_id != holder.holder_id)
        if taking_over_active_holder and not force:
            raise SystemExit(
                f"ticket is claimed by {previous_label or previous_id}. "
                "Ask the human whether to take over; if approved, re-run with "
                "--force --confirm-human-approved-takeover."
            )
        if taking_over_active_holder and force and not confirm_human_approved_takeover:
            raise SystemExit(
                f"ticket is claimed by {previous_label or previous_id}. "
                "--force takeover requires asking a human and passing "
                "--confirm-human-approved-takeover; otherwise fork/new a ticket for parallel work."
            )
        work_log_md = _append_bullet(cv["work_log_md"], _work_log_line("claim", holder, previous_label if previous_id != holder.holder_id else ""))
        conn.execute(
            """UPDATE ticket_meta
               SET owner_id = ?, owner_label = ?, owner_claimed_at = ?,
                   owner_released_at = NULL,
                   lifecycle_state = 'ACTIVE',
                   owner_last_action = 'claim',
                   owner_last_actor_id = ?, owner_last_actor_label = ?,
                   owner_last_action_at = ?,
                   render_revision = render_revision + 1, updated_at = ?
               WHERE ticket_dir = ?""",
            (
                holder.holder_id, holder.holder_label, now,
                holder.holder_id, holder.holder_label, now, now, str(ticket_dir),
            ),
        )
        conn.execute("UPDATE current_view SET work_log_md = ?, updated_at = ? WHERE singleton = 1", (work_log_md, now))
        conn.commit()
        if render:
            do_render(ticket_dir, conn)
        safe_upsert_ticket(ticket_dir)
        return ticket_dir
    finally:
        conn.close()


def release_ticket(
    ticket_path: str,
    *,
    agent_type: str = "",
    session_id: str = "",
    holder_label: str = "",
    force: bool = False,
    render: bool = True,
) -> Path:
    ticket_dir = Path(ticket_path).expanduser().resolve()
    if not ticket_dir.is_dir():
        raise SystemExit(f"ticket directory does not exist: {ticket_dir}")
    require_not_archived(ticket_dir, action="release ticket")
    holder = identity.infer_holder(agent_type=agent_type, session_id=session_id, explicit_label=holder_label)
    conn = open_db(ticket_dir)
    try:
        now = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        meta = conn.execute("SELECT * FROM ticket_meta LIMIT 1").fetchone()
        cv = conn.execute("SELECT * FROM current_view WHERE singleton = 1").fetchone()
        if meta is None or cv is None:
            raise SystemExit("database is missing ticket_meta or current_view row")
        previous_id = str(meta["owner_id"] or "").strip()
        previous_label = str(meta["owner_label"] or "").strip()
        if not previous_id and not force:
            raise SystemExit("ticket is not currently claimed")
        if previous_id and previous_id != holder.holder_id and not force:
            raise SystemExit(
                f"ticket is claimed by {previous_label or previous_id}. "
                "Only the current holder can release it; use --force after confirming handoff."
            )
        work_log_md = _append_bullet(cv["work_log_md"], _work_log_line("release", holder))
        conn.execute(
            """UPDATE ticket_meta
               SET owner_id = NULL, owner_label = NULL, owner_claimed_at = NULL,
                   owner_released_at = ?,
                   lifecycle_state = 'BACKLOG',
                   owner_last_action = 'release',
                   owner_last_actor_id = ?, owner_last_actor_label = ?,
                   owner_last_action_at = ?,
                   render_revision = render_revision + 1, updated_at = ?
               WHERE ticket_dir = ?""",
            (now, holder.holder_id, holder.holder_label, now, now, str(ticket_dir)),
        )
        conn.execute("UPDATE current_view SET work_log_md = ?, updated_at = ? WHERE singleton = 1", (work_log_md, now))
        conn.commit()
        if render:
            do_render(ticket_dir, conn)
        safe_upsert_ticket(ticket_dir)
        return ticket_dir
    finally:
        conn.close()


def _append_bullet(existing: str | None, line: str) -> str:
    bullet = f"- {line}"
    body = (existing or "").rstrip()
    return bullet if not body else f"{body}\n{bullet}"


def cmd_new(args: argparse.Namespace) -> int:
    if not args.topic:
        raise SystemExit("--topic is required")
    result = create_ticket(
        args.topic,
        goal=args.goal,
        short_context=getattr(args, "short_context", ""),
        agent_type=args.agent_type,
        session_id=args.session_id,
        holder_label=args.owner_label,
        backlog=getattr(args, "backlog", False),
    )
    change = WriteChange(
        changed_field="lifecycle",
        old_value="",
        new_value="BACKLOG" if getattr(args, "backlog", False) else "ACTIVE",
    )
    # Contract: stdout receives ONLY the ticket path so that
    # `TICKET_DIR=$(aticket-cli ticket new ...)` and downstream `aticket-cli ticket "$TICKET_DIR" ...`
    # callers keep working. The snapshot block goes to stderr (see
    # reporter.emit_post_write); agents and humans still see it on their
    # terminal, scripts capturing stdout get a clean path.
    print(str(result))
    conn = open_db(result)
    try:
        emit_post_write(conn, result, change, fmt=getattr(args, "format", "plain"))
    finally:
        conn.close()
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    result = claim_ticket(
        args.ticket,
        agent_type=args.agent_type,
        session_id=args.session_id,
        holder_label=args.owner_label,
        force=args.force,
        confirm_human_approved_takeover=getattr(args, "confirm_human_approved_takeover", False),
    )
    print(str(result))
    conn = open_db(result)
    try:
        emit_post_write(conn, result, WriteChange(changed_field="owner", old_value="", new_value="claim"),
                        fmt=getattr(args, "format", "plain"))
    finally:
        conn.close()
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    result = release_ticket(
        args.ticket,
        agent_type=args.agent_type,
        session_id=args.session_id,
        holder_label=args.owner_label,
        force=args.force,
    )
    print(str(result))
    conn = open_db(result)
    try:
        emit_post_write(conn, result, WriteChange(changed_field="owner", old_value="", new_value="release"),
                        fmt=getattr(args, "format", "plain"))
    finally:
        conn.close()
    return 0
