"""Post-write reporter for `aticket-cli` write commands.

After every write-side command (`ticket new`, `ticket <dir> goal`, `context`,
`log`, `add-item`, `archive`, claim), the CLI prints a compact snapshot of
the ticket: what just changed and what the ticket looks like now. The output is
"report-only" — we surface facts (Goal / short context / counts / lifecycle) so
the agent can decide its own next step. We deliberately do NOT prescribe one.

Two output granularities:

- **Frequent appends** (`log`): topic + goal + the
  append summary. Goal/work-log are the agent's high-frequency surface; we
  don't dump every other field on every line.
- **State changes** (`goal` / `context` / `archive` / claim / ...): full snapshot
  with a "changed: <field>  <old> → <new>" line at top.

`add-item` is a hybrid: it gets a recent-items preview (last 5).

Long fields (`goal`, `short_context`) are truncated to ~80 chars; full content is
available in `TICKET.md`.

Format: plain text by default, `--format json` for machine consumption.

Channel: snapshots go to stderr — stdout stays clean for commands whose
output is consumed by shell capture (`TICKET_DIR=$(aticket-cli ticket new ...)`).
The two reporter-emitting commands that also write to stdout (`aticket-cli ticket new`
and `aticket-cli ticket <dir> fork`) print the ticket path on stdout and the snapshot on
stderr; pure-snapshot commands (`goal`, `context`, `log`, …) write the
whole snapshot to stderr and leave stdout empty so they compose cleanly
in pipelines.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .paths import normalize_lifecycle_state
from .reminders import must_remember_items_from_raw

# Visible width for inline values like goal / short_context preview.
_TRUNCATE_WIDTH = 80

# Items preview shown by add-item.
_RECENT_ITEMS = 5


# ── data shape ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WriteChange:
    """What just changed. Exactly one of `changed_field` / `appended_field` set.

    For `changed_field` (state-change commands): record old + new values so the
    snapshot can render a diff line.

    For `appended_field` (log/add-item): record the field
    name, how many entries were added by this call, and the new total. For
    `add-item`, also record whether the URI was already present.
    """
    changed_field: str = ""
    old_value: str = ""
    new_value: str = ""

    appended_field: str = ""
    added_count: int = 0
    new_count: int = 0
    was_already_present: bool | None = None  # add-item only
    delivery_warning: str = ""


@dataclass(frozen=True)
class TicketSnapshot:
    """Read-only snapshot of a ticket's user-visible state at one moment."""
    name: str
    topic: str
    owner: str
    lifecycle: str
    goal: str
    short_context: str
    log_count: int
    artifact_count: int
    must_remember_count: int
    item_count: int
    must_remember: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)


# ── snapshot extraction ────────────────────────────────────────────────


def _count_bullets(body: str | None) -> int:
    """Count lines that look like `- ...` markdown bullets."""
    if not body:
        return 0
    return sum(1 for line in body.splitlines() if line.lstrip().startswith("- "))


def _parse_items(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [str(raw)] if raw else []
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    return [str(parsed)] if parsed else []


def _topic_from_ticket_dir(ticket_dir: Path) -> str:
    """`2026-06-03-foo-bar-baz-122437` → `foo-bar-baz`."""
    name = ticket_dir.name
    # Strip leading YYYY-MM-DD-
    parts = name.split("-", 3)
    if len(parts) < 4:
        return name
    middle = parts[3]
    # Strip trailing -HHMMSS (always 6 digits when present)
    tail = middle.rsplit("-", 1)
    if len(tail) == 2 and tail[1].isdigit() and len(tail[1]) == 6:
        return tail[0]
    return middle


def snapshot_from_db(conn: sqlite3.Connection, ticket_dir: Path) -> TicketSnapshot:
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

    items = _parse_items(cv["items"])

    must_remember = must_remember_items_from_raw(cv["must_remember"])

    return TicketSnapshot(
        name=ticket_dir.name,
        topic=_topic_from_ticket_dir(ticket_dir),
        owner=owner,
        lifecycle=normalize_lifecycle_state(str(meta["lifecycle_state"] or "ACTIVE")),
        goal=str(cv["goal"] or "").strip(),
        short_context=str(cv["short_context"] or "").strip(),
        log_count=_count_bullets(cv["work_log_md"]),
        artifact_count=_count_bullets(cv["artifacts_md"]),
        must_remember_count=len(must_remember),
        item_count=len(items),
        must_remember=must_remember,
        items=items,
    )


# ── formatting helpers ─────────────────────────────────────────────────


def _truncate(value: str, width: int = _TRUNCATE_WIDTH) -> str:
    """Single-line, length-capped preview; '(unset)' for empty.

    Multi-line `goal` / `short_context` collapse to first non-empty line first.
    Truncation appends `... (N chars total)` so the agent knows there's more.
    """
    if not value or not value.strip():
        return "(unset)"
    # First non-empty stripped line.
    first = next((ln.strip() for ln in value.splitlines() if ln.strip()), value.strip())
    total = len(value)
    if len(first) <= width:
        return f'"{first}"' if total == len(first) else f'"{first}" ... ({total} chars total)'
    return f'"{first[:width]}" ... ({total} chars total)'


def _quote_inline(value: str) -> str:
    """Quote single-line values that don't need truncation (lifecycle / owner / etc)."""
    return value if value else "(unset)"


# ── output rendering ───────────────────────────────────────────────────


def _format_plain_minimal(snap: TicketSnapshot, change: WriteChange) -> str:
    """Frequent-append style: topic + goal + the append summary."""
    lines = [
        f"ticket: {snap.name}",
        f"  topic: {snap.topic}",
        f"  goal:  {_truncate(snap.goal)}",
        f"  appended to {change.appended_field} (added {change.added_count}, now {change.new_count} entries)",
    ]
    return "\n".join(lines)


def _format_plain_add_item(snap: TicketSnapshot, change: WriteChange) -> str:
    """add-item: frequent-append with a recent-items preview."""
    presence = ""
    if change.was_already_present is not None:
        presence = f"; was already present: {str(change.was_already_present).lower()}"
    head = (
        f"  appended to items "
        f"(added {change.added_count}, now {change.new_count} entries{presence})"
    )

    total = len(snap.items)
    preview = snap.items[-_RECENT_ITEMS:] if total > 0 else []
    if total > _RECENT_ITEMS:
        preview_header = f"  recent items ({_RECENT_ITEMS} of {total}):"
    else:
        preview_header = "  recent items:"

    lines = [
        f"ticket: {snap.name}",
        f"  topic: {snap.topic}",
        f"  goal:  {_truncate(snap.goal)}",
        head,
    ]
    if preview:
        lines.append(preview_header)
        lines.extend(f"    - {item}" for item in preview)
    return "\n".join(lines)


def _format_plain_full(snap: TicketSnapshot, change: WriteChange) -> str:
    """State-change style: full snapshot with a `changed:` diff line."""
    lines = [f"ticket: {snap.name}"]
    if change.changed_field:
        old = _change_value_for_display(change.changed_field, change.old_value)
        new = _change_value_for_display(change.changed_field, change.new_value)
        # Mark no-op writes explicitly so the agent doesn't read a same-value
        # line as a real transition. Trigger on any same-value diff,
        # including `(unset) → (unset)`: an empty-to-empty transition is
        # still a no-op and should be labelled as such, otherwise composite
        # ops can render misleading bare transitions for fields they didn't
        # actually move.
        marker = "  (no change)" if change.old_value == change.new_value else ""
        lines.append(f"  changed:         {change.changed_field}  {old} → {new}{marker}")
    lines.extend([
        f"  topic:           {snap.topic}",
        f"  owner:           {_quote_inline(snap.owner)}",
        f"  lifecycle:       {snap.lifecycle}",
        f"  goal:            {_truncate(snap.goal)}",
        f"  short_context:   {_truncate(snap.short_context)}",
        f"  log entries:     {snap.log_count}",
        f"  must remember:   {snap.must_remember_count}",
    ])
    lines.extend(f"    {idx}. {item}" for idx, item in enumerate(snap.must_remember, start=1))
    lines.extend([
        f"  artifacts:       {snap.artifact_count}",
        f"  items:           {snap.item_count}",
    ])
    return "\n".join(lines)


def _change_value_for_display(field_name: str, value: str) -> str:
    """How to render old/new in the `changed:` line, per field."""
    if field_name in ("goal", "short_context"):
        return _truncate(value)
    if field_name == "items":
        # The handler may pass either a pre-formatted "N items" string (new_value)
        # or the raw json blob from the db (old_value captured by _update_field).
        # Normalise both shapes to "N items".
        if not value:
            return "0 items"
        if value.endswith(" items"):
            return value
        try:
            parsed = json.loads(value)
            count = len(parsed) if isinstance(parsed, list) else 1
        except (json.JSONDecodeError, TypeError):
            count = 1
        return f"{count} items"
    if not value:
        return "(unset)"
    return f'"{value}"' if len(value) < _TRUNCATE_WIDTH else _truncate(value)


def _format_json(
    conn: sqlite3.Connection,
    ticket_dir: Path,
    snap: TicketSnapshot,
    change: WriteChange,
) -> str:
    # Keep the change dict compact: drop dataclass defaults but preserve
    # booleans (False is meaningful for `was_already_present`).
    change_dict = {}
    defaults = {"changed_field": "", "old_value": "", "new_value": "",
                "appended_field": "", "added_count": 0, "new_count": 0,
                "was_already_present": None, "delivery_warning": ""}
    for k, v in asdict(change).items():
        if v != defaults.get(k):
            change_dict[k] = v
    payload = {
        "ticket": snap.name,
        "change": change_dict,
        "snapshot": {
            "topic": snap.topic,
            "owner": snap.owner,
            "lifecycle": snap.lifecycle,
            "goal": snap.goal,
            "short_context": snap.short_context,
            "log_count": snap.log_count,
            "must_remember_count": snap.must_remember_count,
            "must_remember": snap.must_remember,
            "artifact_count": snap.artifact_count,
            "item_count": snap.item_count,
        },
    }
    if change.appended_field == "items" or change.changed_field == "items":
        payload["snapshot"]["items"] = snap.items
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ── public emit API ────────────────────────────────────────────────────


def emit_post_write(
    conn: sqlite3.Connection,
    ticket_dir: Path,
    change: WriteChange,
    *,
    fmt: str = "plain",
) -> None:
    """Print the post-write snapshot to **stderr**. Called at the tail of
    every write handler.

    `fmt`:
      - "plain" (default): human/agent-readable text block with trailing blank
      - "json": machine-parseable JSON (no trailing blank)

    Snapshots go to stderr so that stdout-capturing callers
    (`TICKET_DIR=$(aticket-cli ticket new ...)`, etc.) get a clean machine-stable
    stdout. Agents and humans still see the snapshot on their terminal (stderr
    is shown by default).
    """
    if fmt not in ("plain", "json"):
        raise ValueError(f"unsupported reporter format: {fmt}")
    snap = snapshot_from_db(conn, ticket_dir)
    if fmt == "json":
        print(_format_json(conn, ticket_dir, snap, change), file=sys.stderr)
        return
    # Plain text dispatch by change shape.
    if change.appended_field == "items":
        print(_format_plain_add_item(snap, change), file=sys.stderr)
    elif change.appended_field:
        print(_format_plain_minimal(snap, change), file=sys.stderr)
    else:
        print(_format_plain_full(snap, change), file=sys.stderr)
    # Trailing blank line for inter-invocation separation.
    print(file=sys.stderr)
