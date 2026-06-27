"""`aticket-cli ticket <dir> fork` — fork an existing ticket into a new independent ticket.

Ported from fork_session.py. The cross-process plumbing is gone: fork is a
special `new` that uses ticket_new's shared scaffold/initialize path, with
source metadata and a point-in-time source snapshot added before initialization.
The rollback guard removes the half-built ticket on any failure.
"""
from __future__ import annotations

import argparse
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import ticket_new
from .atomicio import write_json_atomic, write_text_atomic
from .db import open_db
from .mdparse import parse_ticket_md, title_from_dir
from .paths import split_links
from .render import render_ticket
from .timeutil import now_hms, now_iso

FORBIDDEN_COPY_PREFIXES = ("state", "workspace")
FORBIDDEN_COPY_PATHS = {"TICKET.md", ".gitignore"}
SOURCE_SNAPSHOT_REL = "artifacts/source-ticket-snapshot.md"
DEFAULT_CANONICAL_SOURCE = "source"


@dataclass(frozen=True)
class ForkMaterial:
    copy_paths: list[str]


def _remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _normalize_relpath(raw: str) -> str:
    rel = raw.strip().replace("\\", "/").strip("/")
    if not rel:
        raise SystemExit("copy path cannot be empty")
    parts = [p for p in rel.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise SystemExit(f"copy path must stay inside source ticket: {raw}")
    rel = "/".join(parts)
    if rel in FORBIDDEN_COPY_PATHS:
        raise SystemExit(f"refuse to copy reserved path: {rel}")
    for prefix in FORBIDDEN_COPY_PREFIXES:
        if rel == prefix or rel.startswith(prefix + "/"):
            raise SystemExit(f"refuse to copy runtime-only path: {rel}")
    return rel


def _parse_ticket_text(text: str) -> tuple[str, dict[str, str]]:
    lines = text.splitlines()
    if lines and lines[0].strip() == "<!-- managed-by: sqlite -->":
        lines = lines[1:]
    while lines and (
        lines[-1].startswith("Rendered from DB revision:")
        or lines[-1].startswith("Rendered at:")
        or lines[-1].strip() == "---"
        or lines[-1].strip() == ""
    ):
        lines.pop()
    title = lines[0] if lines else "# Ticket"
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[1:]:
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return title, {name: "\n".join(body).strip("\n") for name, body in sections.items()}


def _source_owner_label(source_dir: Path) -> str | None:
    try:
        conn = open_db(source_dir)
    except SystemExit:
        return None
    try:
        meta = conn.execute("SELECT owner_label FROM ticket_meta LIMIT 1").fetchone()
    finally:
        conn.close()
    if meta is None:
        return None
    return str(meta["owner_label"] or "").strip() or None


def _render_source_if_stale(source_dir: Path) -> None:
    conn = open_db(source_dir)
    try:
        meta = conn.execute("SELECT render_revision, rendered_revision FROM ticket_meta LIMIT 1").fetchone()
    finally:
        conn.close()
    if meta is None:
        raise SystemExit(f"no ticket_meta row found: {source_dir}")
    if meta["render_revision"] != meta["rendered_revision"]:
        render_ticket(source_dir, quiet=True)


def _render_section(name: str, body: str) -> list[str]:
    out = [f"## {name}", ""]
    if body:
        out.extend(body.rstrip().splitlines())
    out.append("")
    return out


def _clean_section_body(body: str, *, placeholder_labels: set[str] | None = None) -> str:
    cleaned: list[str] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped == "-":
            continue
        if placeholder_labels and stripped.startswith("- ") and stripped.endswith(":"):
            if stripped[2:-1].strip() in placeholder_labels:
                continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _merge_bullets(existing: str, extra: list[str], *, placeholder_labels: set[str] | None = None) -> str:
    lines = [line.rstrip() for line in _clean_section_body(existing, placeholder_labels=placeholder_labels).splitlines() if line.strip()]
    lines.extend(extra)
    return "\n".join(lines).strip()


def _section_bullets(body: str) -> list[str]:
    items: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("- "):
            line = line[2:].strip()
        else:
            ordered = re.match(r"^\d+\.\s+(.*)$", line)
            if ordered:
                line = ordered.group(1).strip()
        if line:
            items.append(line)
    return items


def _has_overlap(a: str, b: str) -> bool:
    a_parts = tuple(Path(a).parts)
    b_parts = tuple(Path(b).parts)
    return a_parts == b_parts or a_parts == b_parts[: len(a_parts)] or b_parts == a_parts[: len(b_parts)]


def _validate_material_path_overlaps(copy_paths: list[str]) -> None:
    for i, left in enumerate(copy_paths):
        for right in copy_paths[i + 1:]:
            if _has_overlap(left, right):
                raise SystemExit(f"copy paths must not overlap or contain one another: {left} vs {right}")


def _validate_reserved_material_paths(copy_paths: list[str]) -> None:
    for rel in copy_paths:
        if _has_overlap(rel, SOURCE_SNAPSHOT_REL):
            raise SystemExit(f"refuse to copy reserved fork snapshot path: {rel}")


def _validate_material_sources(source_dir: Path, *, copy_paths: list[str]) -> None:
    hint = "allowed examples live under notes/ or artifacts/"
    for rel in copy_paths:
        if not (source_dir / rel).exists():
            raise SystemExit(f"material source does not exist: {source_dir / rel} ({hint})")


def _copy_paths(source_dir: Path, target_dir: Path, *, copy_paths: list[str]) -> None:
    for rel in copy_paths:
        src, dst = source_dir / rel, target_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def _render_fork_ticket_md(*, target_title, source_dir, target_dir, goal, canonical_source, snapshot_taken_at, source_snapshot_path, source_sections, material) -> str:
    source_owner = _source_owner_label(source_dir)
    links_extra = [
        f"- file://{source_dir}",
        f"- file://{source_dir / 'TICKET.md'}",
        f"- file://{source_dir / 'artifacts'}",
    ]
    links = _merge_bullets(source_sections.get("Items", ""), links_extra)

    decisions_extra = [
        "- This fork starts from a point-in-time rendered snapshot of the source ticket; the source ticket may keep changing after fork.",
        "- Agents may read any ticket, including the source ticket and its artifacts/state, but a fork owner must not modify source-ticket files.",
    ]
    decisions = _merge_bullets(source_sections.get("Decisions", ""), decisions_extra)

    artifact_extra = [f"- Source artifacts dir: {source_dir / 'artifacts'}/"]
    artifact_extra += [f"- Copied into fork: {rel}" for rel in material.copy_paths]
    artifacts = _merge_bullets(source_sections.get("Artifacts", ""), artifact_extra)

    short_context = (
        f"Forked from {source_dir}/ at {snapshot_taken_at}. "
        "Use artifacts/source-ticket-snapshot.md as the point-in-time parent snapshot; "
        "read the source ticket only for additional context."
    )

    fork_lines = [
        f"- Forked from: {source_dir}/",
        f"- Snapshot taken at: {snapshot_taken_at}",
        f"- Source snapshot: {source_snapshot_path}",
        f"- Canonical source after fork: {canonical_source}",
    ]
    if source_owner:
        fork_lines.append(f"- Source ticket owner at fork time: {source_owner}")
    fork_lines += [f"- Copied path: {rel}" for rel in material.copy_paths]

    work_log = f"- {now_hms()}: Forked from {source_dir}/ (snapshot=`{snapshot_taken_at}`, canonical=`{canonical_source}`)"

    out: list[str] = [target_title, "", "Lifecycle: ACTIVE", ""]
    out += _render_section("Goal", _clean_section_body(goal))
    out += _render_section("Short context", short_context)
    out += _render_section("Must remember", _clean_section_body(source_sections.get("Must remember", "").strip()))
    out += _render_section("Scope / Non-goals", _clean_section_body(source_sections.get("Scope / Non-goals", "").strip()))
    out += _render_section("Fork", "\n".join(fork_lines))
    out += _render_section("Items", links)
    out += _render_section("Environment", _clean_section_body(source_sections.get("Environment", "").strip(), placeholder_labels={"Host", "Key env vars"}))
    out += _render_section("Work log", work_log)
    out += _render_section("Decisions", decisions)
    out += _render_section("Artifacts", artifacts)
    return "\n".join(out).rstrip() + "\n"


def cmd_fork(args: argparse.Namespace) -> int:
    goal = str(getattr(args, "goal", "") or "").strip()
    if not goal:
        raise SystemExit("--goal is required and cannot be empty")
    source_dir = Path(args.ticket).expanduser().resolve()
    if not source_dir.is_dir():
        raise SystemExit(f"source ticket dir not found: {source_dir}")
    if not (source_dir / "TICKET.md").exists():
        raise SystemExit(f"source ticket missing TICKET.md: {source_dir / 'TICKET.md'}")
    _render_source_if_stale(source_dir)

    copy_paths = [_normalize_relpath(p) for p in args.copy_path]
    _validate_material_path_overlaps(copy_paths)
    _validate_reserved_material_paths(copy_paths)
    _validate_material_sources(source_dir, copy_paths=copy_paths)

    target_dir: Path | None = None
    try:
        holder = ticket_new.identity.infer_holder(
            agent_type=getattr(args, "agent_type", ""),
            session_id=getattr(args, "session_id", ""),
            explicit_label=getattr(args, "owner_label", ""),
        )
        target_dir = ticket_new.scaffold_ticket(args.topic)

        snapshot_taken_at = now_iso()
        material = ForkMaterial(copy_paths=copy_paths)
        _copy_paths(source_dir, target_dir, copy_paths=copy_paths)

        target_title = title_from_dir(target_dir)
        source_ticket_text = (source_dir / "TICKET.md").read_text(encoding="utf-8")
        source_snapshot_path = target_dir / SOURCE_SNAPSHOT_REL
        write_text_atomic(source_snapshot_path, source_ticket_text)

        _, source_sections = parse_ticket_md(source_dir / "TICKET.md")
        rendered = _render_fork_ticket_md(
            target_title=target_title, source_dir=source_dir, target_dir=target_dir,
            goal=goal,
            canonical_source=DEFAULT_CANONICAL_SOURCE, snapshot_taken_at=snapshot_taken_at,
            source_snapshot_path=source_snapshot_path,
            source_sections=source_sections, material=material,
        )
        _, rs = _parse_ticket_text(rendered)
        items, links_extra_md = split_links(rs.get("Items", ""))

        fork_metadata = {
            "canonical_source_after_fork": DEFAULT_CANONICAL_SOURCE,
            "copied_paths": copy_paths,
            "forked_from": str(source_dir),
            "snapshot_taken_at": snapshot_taken_at,
            "source_ticket_snapshot_path": str(source_snapshot_path),
        }
        source_owner = _source_owner_label(source_dir)
        if source_owner:
            fork_metadata["source_ticket_owner_at_fork_time"] = source_owner

        write_json_atomic(target_dir / "state" / "fork.json", dict(fork_metadata))

        ticket_new.initialize_new_ticket(
            target_dir,
            ticket_new.claimed_init_data(
                holder,
                claimed_at=snapshot_taken_at,
                title=target_title,
                source_ticket_dir=str(source_dir),
                canonical_source=DEFAULT_CANONICAL_SOURCE,
                fork_metadata=fork_metadata,
                lifecycle_state="ACTIVE",
                goal=rs.get("Goal", ""),
                short_context=rs.get("Short context", ""),
                must_remember=tuple(_section_bullets(rs.get("Must remember", ""))),
                scope_non_goals=rs.get("Scope / Non-goals", ""),
                items=tuple(items),
                links_extra_md=links_extra_md,
                decisions_md=rs.get("Decisions", ""),
                artifacts_md=rs.get("Artifacts", ""),
                work_log_md=rs.get("Work log", ""),
                env_md=rs.get("Environment", ""),
            ),
            render=True,
            quiet=True,
        )
    except BaseException:
        if target_dir is not None:
            _remove_tree(target_dir)
        raise

    # Past this point the fork is fully initialized and durable on disk; do
    # NOT roll it back if the post-write snapshot fails. A3: the snapshot
    # emit must live outside the BaseException cleanup scope so a render /
    # snapshot bug cannot silently delete the fork after the path has been
    # printed (callers reading stdout would already have signalled success).
    #
    # Contract: stdout receives ONLY the fork path (so `FORK_DIR=$(ticket
    # fork ...)` and downstream `--ticket "$FORK_DIR"` callers keep working).
    # The snapshot block goes to stderr; humans/agents still see it on the
    # terminal, scripts capturing stdout get a clean path.
    print(str(target_dir))
    from .db import open_db as _open_db  # avoid top-level cycle risk
    from .reporter import WriteChange as _WC, emit_post_write as _emit
    try:
        conn = _open_db(target_dir)
        try:
            # old_value="" renders as `(unset)` in the reporter — factual for a
            # newly created ticket (consistent with `aticket-cli ticket new` which uses the
            # same convention after round-8 audit #1 fix).
            _emit(conn, target_dir, _WC(changed_field="lifecycle", old_value="", new_value="ACTIVE"),
                  fmt=getattr(args, "format", "plain"))
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — degrade snapshot, never undo the fork
        # Snapshot is best-effort UX; never let it retract the success signal.
        # Log degradation to stderr so it stays out of stdout-captured path.
        import sys as _sys
        print(f"(snapshot failed: {exc})", file=_sys.stderr)
    return 0
