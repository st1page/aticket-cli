"""Squash archived tickets into a continuing larger ticket."""
import json
from pathlib import Path

import pytest

from conftest import make_ticket, owner_args, read_db, run, seed_legacy_artifacts


def _archive(ticket_dir):
    assert run(["ticket", str(ticket_dir), "archive"]) == 0


def _make_archived_source(topic: str, *, goal: str = "", must_remember: tuple[str, ...] = ()):
    ticket_dir = make_ticket(topic)
    if goal:
        assert run(["ticket", str(ticket_dir), "goal", goal]) == 0
    for item in must_remember:
        assert run(["ticket", str(ticket_dir), "remember", item]) == 0
    assert run(["ticket", str(ticket_dir), "context", f"{topic} done"]) == 0
    assert run(["ticket", str(ticket_dir), "log", f"{topic} source log"]) == 0
    assert run(["ticket", str(ticket_dir), "add-item", f"https://example.test/{topic}"]) == 0
    artifact_file = ticket_dir / "artifacts" / f"{topic}.txt"
    artifact_file.write_text(f"{topic} artifact\n", encoding="utf-8")
    seed_legacy_artifacts(
        ticket_dir,
        f"{topic} result",
        f"{topic} artifact file: artifacts/{topic}.txt",
        f"{topic} artifact question: artifacts/{topic}.txt?",
    )
    _archive(ticket_dir)
    return ticket_dir


def test_squash_archived_tickets_creates_active_target_and_source_refs(tickets_root, capsys):
    a = _make_archived_source("tiny-a", goal="tiny a goal")
    b = _make_archived_source("tiny-b", goal="tiny b goal")
    capsys.readouterr()

    assert run([
        "tickets", "squash",
        str(a), str(b),
        "--topic", "combined-work",
        "--goal", "Continue combined work",
        "--summary", "A and B were one larger intent",
        "--next", "Keep working here",
        *owner_args(), "--owner-label", "squasher",
    ]) == 0
    target = Path(capsys.readouterr().out.strip())

    meta, cv = read_db(target)
    assert meta["lifecycle_state"] == "ACTIVE"
    assert meta["owner_label"] == "squasher"
    assert cv["goal"] == "Continue combined work"
    assert "A and B were one larger intent" in cv["short_context"]
    assert "Keep working here" in cv["short_context"]
    assert str(a.as_uri()) in cv["items"]
    assert str(b.as_uri()) in cv["items"]
    assert "https://example.test/tiny-a" in cv["items"]
    assert "https://example.test/tiny-b" in cv["items"]
    assert "Squashed source ticket" in cv["artifacts_md"]
    assert f"tiny-a artifact file: {(a / 'artifacts' / 'tiny-a.txt').as_uri()}" in cv["artifacts_md"]
    assert f"tiny-b artifact file: {(b / 'artifacts' / 'tiny-b.txt').as_uri()}" in cv["artifacts_md"]
    assert f"tiny-a artifact question: {(a / 'artifacts' / 'tiny-a.txt').as_uri()}?" in cv["artifacts_md"]
    assert f"tiny-b artifact question: {(b / 'artifacts' / 'tiny-b.txt').as_uri()}?" in cv["artifacts_md"]
    assert "tiny-a.txt%3F" not in cv["artifacts_md"]
    assert "tiny-b.txt%3F" not in cv["artifacts_md"]
    assert f"[squashed from {a.as_uri()}]" in cv["work_log_md"]
    assert "tiny-a source log" in cv["work_log_md"]
    assert f"[squashed from {b.as_uri()}]" in cv["work_log_md"]
    assert "tiny-b source log" in cv["work_log_md"]
    assert "continuing work container" in cv["decisions_md"]

    metadata = json.loads((target / "state" / "squash.json").read_text(encoding="utf-8"))
    assert metadata["kind"] == "ticket-squash"
    assert metadata["target_lifecycle_state"] == "ACTIVE"
    assert [Path(item["ticket_dir"]).name for item in metadata["sources"]] == [a.name, b.name]
    for source in (a, b):
        snapshot = target / "artifacts" / "squashed-source-snapshots" / f"{source.name}.md"
        assert snapshot.is_file()
        assert "tiny-" in snapshot.read_text(encoding="utf-8")

        source_meta, source_cv = read_db(source)
        assert source_meta["lifecycle_state"] == "ARCHIVED"
        assert target.as_uri() in source_cv["items"]
        assert f"Squashed into ticket: {target.as_uri()}" in source_cv["artifacts_md"]
        assert "Squashed into ticket" in source_cv["work_log_md"]
        assert source_meta["squashed_into_ticket_dir"] == str(target)
        assert source_meta["squashed_into_ticket_uri"] == target.as_uri()
        assert source_meta["squashed_into_at"]
        assert f"- {target.as_uri()}" in (source / "TICKET.md").read_text(encoding="utf-8")
        assert f"Squashed into: {target.as_uri()}" in (source / "TICKET.md").read_text(encoding="utf-8")
        assert not (source / "state" / "squashed_into.json").exists()


def test_squash_can_create_backlog_target(tickets_root, capsys):
    a = _make_archived_source("backlog-a")
    b = _make_archived_source("backlog-b")
    capsys.readouterr()

    assert run([
        "tickets", "squash",
        str(a), str(b),
        "--topic", "combined-backlog",
        "--goal", "Resume later",
        "--backlog",
    ]) == 0
    target = Path(capsys.readouterr().out.strip())

    meta, cv = read_db(target)
    assert meta["lifecycle_state"] == "BACKLOG"
    assert meta["owner_id"] is None
    assert "Squashed 2 archived tickets" in cv["short_context"]
    assert target.as_uri() in read_db(a)[1]["items"]


def test_squash_inherits_source_must_remember_entries_in_order(tickets_root, capsys):
    a = _make_archived_source(
        "remember-a",
        must_remember=("Preflight: verify worktree", "Invariant: keep source links"),
    )
    b = _make_archived_source(
        "remember-b",
        must_remember=("Invariant: keep source links", "Principle: preserve continuing context"),
    )
    capsys.readouterr()

    assert run([
        "tickets", "squash",
        str(a), str(b),
        "--topic", "combined-remember",
        "--goal", "Continue remembered work",
        *owner_args(),
    ]) == 0
    target = Path(capsys.readouterr().out.strip())

    _, cv = read_db(target)
    assert json.loads(cv["must_remember"]) == [
        "Preflight: verify worktree",
        "Invariant: keep source links",
        "Principle: preserve continuing context",
    ]
    rendered = (target / "TICKET.md").read_text(encoding="utf-8")
    assert "## Must remember" in rendered
    assert "1. Preflight: verify worktree" in rendered
    assert "2. Invariant: keep source links" in rendered
    assert "3. Principle: preserve continuing context" in rendered


def test_squash_rejects_too_many_source_must_remember_entries(tickets_root):
    a = _make_archived_source(
        "too-many-a",
        must_remember=tuple(f"A invariant {idx}" for idx in range(1, 10)),
    )
    b = _make_archived_source(
        "too-many-b",
        must_remember=tuple(f"B invariant {idx}" for idx in range(1, 9)),
    )

    with pytest.raises(SystemExit, match="squashed Must remember would exceed 16 entries"):
        run([
            "tickets", "squash",
            str(a), str(b),
            "--topic", "too-many-remember",
            "--goal", "Should fail",
            *owner_args(),
        ])


def test_squash_rejects_owner_flags_for_unclaimed_target(tickets_root):
    a = _make_archived_source("owner-a")
    b = _make_archived_source("owner-b")

    with pytest.raises(SystemExit, match="do not pass owner identity flags"):
        run([
            "tickets", "squash",
            str(a), str(b),
            "--topic", "bad-owner",
            "--goal", "Bad owner",
            "--backlog",
            *owner_args(),
        ])


def test_squash_rejects_non_archived_source(tickets_root):
    active = make_ticket("not-archived")
    archived = _make_archived_source("archived")

    with pytest.raises(SystemExit, match="must be ARCHIVED"):
        run([
            "tickets", "squash",
            str(active), str(archived),
            "--topic", "bad-squash",
            "--goal", "Should fail",
            *owner_args(),
        ])


def test_squash_rejects_already_squashed_source(tickets_root, capsys):
    a = _make_archived_source("once-a")
    b = _make_archived_source("once-b")
    c = _make_archived_source("once-c")
    capsys.readouterr()

    assert run([
        "tickets", "squash",
        str(a), str(b),
        "--topic", "first-squash",
        "--goal", "First squash",
        *owner_args(),
    ]) == 0

    with pytest.raises(SystemExit, match="already squashed"):
        run([
            "tickets", "squash",
            str(a), str(c),
            "--topic", "second-squash",
            "--goal", "Second squash",
            *owner_args(),
        ])


def test_archived_squash_target_can_be_squashed_again(tickets_root, capsys):
    a = _make_archived_source("chain-a")
    b = _make_archived_source("chain-b")
    c = _make_archived_source("chain-c")
    capsys.readouterr()

    assert run([
        "tickets", "squash",
        str(a), str(b),
        "--topic", "first-chain",
        "--goal", "First combined work",
        "--summary", "A and B first",
        *owner_args(),
    ]) == 0
    first = Path(capsys.readouterr().out.strip())
    assert run(["ticket", str(first), "archive"]) == 0

    assert run([
        "tickets", "squash",
        str(first), str(c),
        "--topic", "second-chain",
        "--goal", "Second combined work",
        "--summary", "First target and C",
        *owner_args(),
    ]) == 0
    second = Path(capsys.readouterr().out.strip())

    second_meta, second_cv = read_db(second)
    assert second_meta["lifecycle_state"] == "ACTIVE"
    assert first.as_uri() in second_cv["items"]
    assert c.as_uri() in second_cv["items"]
    assert "https://example.test/chain-a" in second_cv["items"]
    assert "https://example.test/chain-b" in second_cv["items"]
    assert "https://example.test/chain-c" in second_cv["items"]
    assert f"[squashed from {first.as_uri()}]" in second_cv["work_log_md"]
    assert "chain-a source log" in second_cv["work_log_md"]
    assert "chain-c source log" in second_cv["work_log_md"]

    second_metadata = json.loads((second / "state" / "squash.json").read_text(encoding="utf-8"))
    assert {Path(item["ticket_dir"]).name for item in second_metadata["sources"]} == {first.name, c.name}
    first_meta, first_cv = read_db(first)
    assert first_meta["squashed_into_ticket_dir"] == str(second)
    assert first_meta["squashed_into_ticket_uri"] == second.as_uri()
    assert second.as_uri() in first_cv["items"]
    assert not (first / "state" / "squashed_into.json").exists()


def test_squash_rolls_back_source_refs_when_annotation_fails(tickets_root, monkeypatch, capsys):
    from ticket_cli import squash

    a = _make_archived_source("rollback-a")
    b = _make_archived_source("rollback-b")
    before_dirs = set((tickets_root / "tickets").iterdir())
    original_annotate_source = squash._annotate_source

    def fail_second_source_annotation(source, *, target_dir, squashed_at):
        if source.path == b:
            raise RuntimeError("source annotation failed")
        original_annotate_source(source, target_dir=target_dir, squashed_at=squashed_at)

    monkeypatch.setattr(squash, "_annotate_source", fail_second_source_annotation)

    with pytest.raises(RuntimeError, match="source annotation failed"):
        run([
            "tickets", "squash",
            str(a), str(b),
            "--topic", "rollback-squash",
            "--goal", "Should roll back",
            *owner_args(),
        ])

    assert set((tickets_root / "tickets").iterdir()) == before_dirs
    for source in (a, b):
        assert not (source / "state" / "squashed_into.json").exists()
        meta, cv = read_db(source)
        assert meta["squashed_into_ticket_dir"] is None
        assert meta["squashed_into_ticket_uri"] is None
        assert meta["squashed_into_at"] is None
        assert "Squashed into ticket" not in cv["artifacts_md"]
        assert "Squashed into ticket" not in cv["work_log_md"]
        assert all("rollback-squash" not in item for item in json.loads(cv["items"]))

    assert run(["tickets", "search", "--query", "rollback-squash", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
