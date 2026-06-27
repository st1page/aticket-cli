"""Fork: inheritance, source snapshots, copy path validation, rollback."""
import json
from pathlib import Path

import pytest

from conftest import DEFAULT_AGENT_TYPE, DEFAULT_OWNER_SESSION_ID, create_codex_session, make_stale_goal, make_ticket, owner_args, owner_uri, read_db, run
from ticket_cli import fork as fork_mod


def _fork(tickets_root, source, **kw):
    import argparse

    ns = argparse.Namespace(
        ticket=str(source), topic=kw.get("topic", "forked"),
        goal=kw.get("goal", "forked child goal"),
        copy_path=kw.get("copy_path", []),
        agent_type=kw.get("agent_type", DEFAULT_AGENT_TYPE),
        session_id=kw.get("session_id", DEFAULT_OWNER_SESSION_ID),
        owner_label=kw.get("owner_label", ""),
    )
    fork_mod.cmd_fork(ns)
    # newest forked dir under tickets/
    tickets = sorted((Path(tickets_root) / "tickets").iterdir(), key=lambda p: p.stat().st_mtime)
    return tickets[-1]


def test_fork_uses_explicit_goal_and_writes_metadata(tickets_root):
    src = make_ticket("src")
    (Path(src) / "notes" / "n.md").write_text("note")
    source_md_at_fork = (Path(src) / "TICKET.md").read_text()
    f = _fork(tickets_root, src, goal="the fork goal", copy_path=["notes/n.md"])
    _, cv = read_db(f)
    assert "the fork goal" in cv["goal"]
    meta, _ = read_db(f)
    fm = json.loads(meta["fork_metadata_json"])
    assert fm["forked_from"] == str(Path(src))
    assert fm["copied_paths"] == ["notes/n.md"]
    snapshot_path = f / "artifacts" / "source-ticket-snapshot.md"
    assert fm["source_ticket_snapshot_path"] == str(snapshot_path)
    assert snapshot_path.read_text() == source_md_at_fork
    md = (f / "TICKET.md").read_text()
    assert f"- Source snapshot: {snapshot_path}" in md
    assert meta["owner_id"]
    assert meta["owner_last_action"] == "claim"
    assert "Owner: claim by " in md
    assert (f / "notes" / "n.md").read_text() == "note"


def test_fork_accepts_explicit_agent_session(tickets_root, monkeypatch, tmp_path):
    src = make_ticket("src")
    session_id = "88888888-8888-4888-8888-888888888888"
    create_codex_session(tmp_path / "codex-sessions", session_id)
    monkeypatch.setenv("CODEX_JSONL_ROOT", str(tmp_path / "codex-sessions"))

    assert run([
        "ticket", str(src), "fork", "--topic", "explicit-owner",
        "--goal", "explicit fork goal",
        *owner_args(session_id), "--owner-label", "explicit holder",
    ]) == 0

    f = next(d for d in (tickets_root / "tickets").iterdir() if "explicit-owner" in d.name)
    meta, _ = read_db(f)
    assert meta["owner_id"] == owner_uri(session_id)
    assert meta["owner_label"] == "explicit holder"


def test_fork_json_reports_unread_messages_from_source_ticket(tickets_root, capsys):
    src = make_ticket("src-json-notice")
    run([
        "message", "send",
        "--ticket", str(src),
        "source notice before fork",
        *owner_args(),
    ])
    capsys.readouterr()

    assert run([
        "ticket", str(src), "fork",
        "--topic", "json-fork",
        "--goal", "json fork goal",
        *owner_args(),
        "--format", "json",
    ]) == 0

    cap = capsys.readouterr()
    fork_dir = Path(cap.out.strip())
    assert fork_dir.is_dir()
    payload = json.loads(cap.err)
    assert payload["ticket"] == fork_dir.name
    assert payload["snapshot"]["goal"] == "json fork goal"
    assert payload["unread_messages"]["ticket"] == str(Path(src).resolve())
    assert payload["unread_messages"]["count"] == 1
    assert payload["unread_messages"]["messages"][0]["message"] == "source notice before fork"
    assert "message: ticket has" not in cap.err


def test_fork_source_snapshot_is_point_in_time(tickets_root):
    src = make_ticket("src")
    run(["ticket", str(src), "goal", "source goal at fork"])
    f = _fork(tickets_root, src)
    snapshot_path = f / "artifacts" / "source-ticket-snapshot.md"

    run(["ticket", str(src), "goal", "source goal after fork"])

    snapshot = snapshot_path.read_text()
    assert "source goal at fork" in snapshot
    assert "source goal after fork" not in snapshot


def test_fork_preserves_unknown_scheme_items(tickets_root):
    src = make_ticket("src")
    run(["ticket", str(src), "add-item", "agents://review/thread"])
    run(["ticket", str(src), "add-item", "https://github.example.com/org/foo/pull/1"])

    f = _fork(tickets_root, src)
    _, cv = read_db(f)
    items = json.loads(cv["items"])
    assert "agents://review/thread" in items
    assert "https://github.example.com/org/foo/pull/1" in items

    md = (f / "TICKET.md").read_text()
    assert md.count("- agents://review/thread") == 1


def test_fork_inherits_must_remember_entries(tickets_root):
    src = make_ticket("src-remember")
    run(["ticket", str(src), "remember", "Principle: never edit root checkout"])
    run(["ticket", str(src), "remember", "Preflight: confirm active ticket before commands"])

    f = _fork(tickets_root, src)
    _, cv = read_db(f)

    assert json.loads(cv["must_remember"]) == [
        "Principle: never edit root checkout",
        "Preflight: confirm active ticket before commands",
    ]
    md = (f / "TICKET.md").read_text(encoding="utf-8")
    assert "## Must remember" in md
    assert "1. Principle: never edit root checkout" in md


def test_fork_accepts_ordered_must_remember_snapshot(tickets_root):
    src = make_ticket("src-ordered-remember")
    run(["ticket", str(src), "remember", "First ordered invariant"])
    run(["ticket", str(src), "remember", "Second ordered invariant"])

    f = _fork(tickets_root, src)
    _, cv = read_db(f)

    assert json.loads(cv["must_remember"]) == [
        "First ordered invariant",
        "Second ordered invariant",
    ]


def test_fork_accepts_legacy_bullet_must_remember_snapshot(tickets_root):
    src = make_ticket("src-legacy-remember")
    run(["ticket", str(src), "remember", "First legacy invariant"])
    run(["ticket", str(src), "remember", "Second legacy invariant"])

    ticket_md = Path(src) / "TICKET.md"
    text = ticket_md.read_text(encoding="utf-8")
    text = text.replace(
        "1. First legacy invariant\n2. Second legacy invariant",
        "- First legacy invariant\n- Second legacy invariant",
    )
    ticket_md.write_text(text, encoding="utf-8")

    f = _fork(tickets_root, src)
    _, cv = read_db(f)

    assert json.loads(cv["must_remember"]) == [
        "First legacy invariant",
        "Second legacy invariant",
    ]


def test_fork_rejects_forbidden_copy_path(tickets_root):
    src = make_ticket("src")
    with pytest.raises(SystemExit):
        _fork(tickets_root, src, copy_path=["state/ticket.sqlite3"])


def test_fork_rejects_overlapping_paths(tickets_root):
    src = make_ticket("src")
    (Path(src) / "notes" / "sub").mkdir(parents=True)
    (Path(src) / "notes" / "sub" / "f.md").write_text("x")
    with pytest.raises(SystemExit):
        _fork(tickets_root, src, copy_path=["notes", "notes/sub"])


def test_fork_rejects_reserved_source_snapshot_material_path(tickets_root):
    src = make_ticket("src")
    reserved = Path(src) / "artifacts" / "source-ticket-snapshot.md"
    reserved.write_text("source artifact with same name")

    with pytest.raises(SystemExit) as excinfo:
        _fork(tickets_root, src, copy_path=["artifacts/source-ticket-snapshot.md"])

    assert "reserved fork snapshot path" in str(excinfo.value)


def test_fork_rollback_removes_target_on_failure(tickets_root):
    src = make_ticket("src")
    before = set((Path(tickets_root) / "tickets").iterdir())
    with pytest.raises(SystemExit):
        _fork(tickets_root, src, copy_path=["does/not/exist"])
    after = set((Path(tickets_root) / "tickets").iterdir())
    assert before == after  # no half-built fork left behind


def test_fork_renders_stale_source_before_snapshot(tickets_root):
    src = make_ticket("src")
    assert make_stale_goal(src, "db-only goal") == 0

    f = _fork(tickets_root, src)

    snapshot = (f / "artifacts" / "source-ticket-snapshot.md").read_text()
    assert "db-only goal" in snapshot
    meta, _ = read_db(src)
    assert meta["render_revision"] == meta["rendered_revision"]
