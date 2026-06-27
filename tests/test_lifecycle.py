"""Lifecycle + current-view: init shape, automatic rendering, append atomicity."""
import json
import os
import sqlite3
from pathlib import Path

import pytest

from conftest import DEFAULT_AGENT_TYPE, DEFAULT_OWNER_SESSION_ID, DEFAULT_OWNER_URI, create_claude_session, create_codex_session, make_ticket, owner_args, owner_uri, read_db, run


def drop_created_at_column(ticket_dir):
    conn = sqlite3.connect(Path(ticket_dir) / "state" / "ticket.sqlite3")
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(ticket_meta)").fetchall()]
        kept_columns = [column for column in columns if column != "created_at"]
        conn.execute(f"CREATE TABLE ticket_meta_old AS SELECT {', '.join(kept_columns)} FROM ticket_meta")
        conn.execute("DROP TABLE ticket_meta")
        conn.execute("ALTER TABLE ticket_meta_old RENAME TO ticket_meta")
        conn.execute("UPDATE ticket_meta SET schema_version = 4")
        conn.commit()
    finally:
        conn.close()


def test_create_initializes_two_row_db(tickets_root):
    t = make_ticket()
    meta, cv = read_db(t)
    assert meta["lifecycle_state"] == "ACTIVE"
    assert meta["schema_version"] == 8
    assert meta["created_at"]
    assert "." not in meta["created_at"]
    assert meta["squashed_into_ticket_dir"] is None
    assert meta["squashed_into_ticket_uri"] is None
    assert meta["squashed_into_at"] is None
    assert meta["owner_id"] == DEFAULT_OWNER_URI
    assert meta["owner_last_action"] == "claim"
    assert cv["singleton"] == 1
    assert (Path(t) / "TICKET.md").read_text().splitlines()[0] == "<!-- managed-by: sqlite -->"
    # greenfield dirs present, deprecated ones absent
    for sub in ("notes", "artifacts", "workspace", "state"):
        assert (Path(t) / sub).is_dir()
    assert not (Path(t) / "snapshots").exists()
    assert not (Path(t) / "tmp").exists()


def test_create_backlog_ticket_starts_unclaimed(tickets_root, capsys):
    assert run([
        "ticket", "new",
        "--topic", "later-work",
        "--goal", "Do later work",
        "--backlog",
    ]) == 0
    ticket_dir = Path(capsys.readouterr().out.strip())

    meta, cv = read_db(ticket_dir)
    assert meta["lifecycle_state"] == "BACKLOG"
    assert meta["owner_id"] is None
    assert meta["owner_last_action"] is None
    assert cv["work_log_md"] == ""

    md = (ticket_dir / "TICKET.md").read_text()
    assert "Lifecycle: BACKLOG" in md
    assert "Owner:" not in md
    assert "Claimed ticket by" not in md


def test_create_backlog_rejects_owner_flags(tickets_root):
    with pytest.raises(SystemExit, match="do not pass owner identity flags"):
        run([
            "ticket", "new",
            "--topic", "later-work",
            "--goal", "Do later work",
            "--backlog",
            "--agent-type", "codex",
            "--session-id", DEFAULT_OWNER_SESSION_ID,
        ])

    with pytest.raises(SystemExit, match="do not pass owner identity flags"):
        run([
            "ticket", "new",
            "--topic", "later-work",
            "--goal", "Do later work",
            "--backlog",
            "--owner-label", "ignored owner",
        ])


def test_open_existing_v4_db_upgrades_schema_version_notices_and_created_at(tickets_root):
    t = make_ticket()
    drop_created_at_column(t)
    conn = sqlite3.connect(Path(t) / "state" / "ticket.sqlite3")
    try:
        conn.execute("DROP TABLE notices")
        conn.commit()
    finally:
        conn.close()

    from ticket_cli.db import open_db

    conn = open_db(Path(t))
    try:
        meta = conn.execute("SELECT * FROM ticket_meta LIMIT 1").fetchone()
        meta_columns = {row["name"] for row in conn.execute("PRAGMA table_info(ticket_meta)").fetchall()}
        notices_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'notices'"
        ).fetchone()
        notice_columns = {row["name"] for row in conn.execute("PRAGMA table_info(notices)").fetchall()}
        current_view_columns = {row["name"] for row in conn.execute("PRAGMA table_info(current_view)").fetchall()}
    finally:
        conn.close()

    assert meta["schema_version"] == 8
    assert "created_at" in meta_columns
    assert "squashed_into_ticket_dir" in meta_columns
    assert "squashed_into_ticket_uri" in meta_columns
    assert "squashed_into_at" in meta_columns
    assert meta["created_at"]
    assert notices_table is not None
    assert "archived_delivery" in notice_columns
    assert "must_remember" in current_view_columns


def test_open_existing_v7_db_adds_must_remember_list_column(tickets_root):
    t = make_ticket("v7-upgrade")
    conn = sqlite3.connect(Path(t) / "state" / "ticket.sqlite3")
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(current_view)").fetchall()]
        kept_columns = [column for column in columns if column != "must_remember"]
        conn.execute(f"CREATE TABLE current_view_old AS SELECT {', '.join(kept_columns)} FROM current_view")
        conn.execute("DROP TABLE current_view")
        conn.execute("ALTER TABLE current_view_old RENAME TO current_view")
        conn.execute("UPDATE ticket_meta SET schema_version = 7")
        conn.commit()
    finally:
        conn.close()

    from ticket_cli.db import open_db

    conn = open_db(Path(t))
    try:
        meta = conn.execute("SELECT * FROM ticket_meta LIMIT 1").fetchone()
        cv = conn.execute("SELECT * FROM current_view WHERE singleton = 1").fetchone()
        current_view_columns = {row["name"] for row in conn.execute("PRAGMA table_info(current_view)").fetchall()}
    finally:
        conn.close()

    assert meta["schema_version"] == 8
    assert "must_remember" in current_view_columns
    assert cv["must_remember"] is None


def test_open_existing_branch_db_migrates_must_remember_md_to_list(tickets_root):
    t = make_ticket("branch-upgrade")
    conn = sqlite3.connect(Path(t) / "state" / "ticket.sqlite3")
    try:
        conn.execute("ALTER TABLE current_view ADD COLUMN must_remember_md TEXT")
        conn.execute(
            "UPDATE current_view SET must_remember = '', must_remember_md = ? WHERE singleton = 1",
            ("- Principle: keep this\n- Preflight: and this",),
        )
        conn.commit()
    finally:
        conn.close()

    from ticket_cli.db import open_db

    conn = open_db(Path(t))
    try:
        cv = conn.execute("SELECT * FROM current_view WHERE singleton = 1").fetchone()
    finally:
        conn.close()

    assert json.loads(cv["must_remember"]) == ["Principle: keep this", "Preflight: and this"]


def test_open_db_rejects_stale_ticket_dir(tickets_root):
    from ticket_cli.db import open_db

    t = make_ticket("open-stale-path")
    stale_ticket_dir = f"/code/tsshi/agent-tickets/tickets/{Path(t).name}"
    conn = sqlite3.connect(Path(t) / "state" / "ticket.sqlite3")
    try:
        conn.execute("UPDATE ticket_meta SET ticket_dir = ?", (stale_ticket_dir,))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SystemExit, match="ticket_meta.ticket_dir does not match"):
        open_db(Path(t))


def test_change_goal_bumps_revision_and_renders(tickets_root):
    t = make_ticket()
    _, cv0 = read_db(t)
    assert run(["ticket", str(t), "goal", "ship it"]) == 0
    meta, cv = read_db(t)
    assert cv["goal"] == "ship it"
    # render kept views fresh
    assert meta["render_revision"] == meta["rendered_revision"]
    assert "ship it" in (Path(t) / "TICKET.md").read_text()


def test_claim_renders_quietly_and_keeps_view_fresh(tickets_root, capsys):
    t = make_ticket()
    capsys.readouterr()

    assert run([
        "ticket", str(t), "claim",
        *owner_args(),
        "--owner-label", "codex test",
        "--force",
    ]) == 0
    out = capsys.readouterr().out
    assert "rendered (" not in out
    assert str(t) in out

    meta, cv = read_db(t)
    assert meta["render_revision"] == meta["rendered_revision"]
    assert meta["owner_id"] == DEFAULT_OWNER_URI
    assert "Claimed ticket by `codex test`" in cv["work_log_md"]

    md = (Path(t) / "TICKET.md").read_text()
    assert "Claimed ticket by `codex test`" in md
    assert "Owner: claim by codex test at " in md


def test_claim_can_skip_render_without_losing_db_update(tickets_root):
    from ticket_cli.ticket_new import claim_ticket

    t = make_ticket()
    claim_ticket(
        str(t),
        agent_type=DEFAULT_AGENT_TYPE,
        session_id=DEFAULT_OWNER_SESSION_ID,
        holder_label="codex test",
        force=True,
        render=False,
    )

    meta, cv = read_db(t)
    assert meta["render_revision"] == 2
    assert meta["rendered_revision"] == 1
    assert "Claimed ticket by `codex test`" in cv["work_log_md"]

    md = (Path(t) / "TICKET.md").read_text()
    assert "Claimed ticket by `codex test`" not in md


def test_force_claim_requires_human_approval_for_different_active_holder(tickets_root):
    t = make_ticket()
    takeover_session_id = "22222222-2222-4222-8222-222222222222"
    create_codex_session(Path(os.environ["CODEX_JSONL_ROOT"]), takeover_session_id)

    with pytest.raises(SystemExit, match="--confirm-human-approved-takeover"):
        run([
            "ticket", str(t), "claim",
            *owner_args(takeover_session_id),
            "--owner-label", "takeover holder",
            "--force",
        ])

    meta, _ = read_db(t)
    assert meta["owner_id"] == DEFAULT_OWNER_URI

    assert run([
        "ticket", str(t), "claim",
        *owner_args(takeover_session_id),
        "--owner-label", "takeover holder",
        "--force",
        "--confirm-human-approved-takeover",
    ]) == 0

    meta, cv = read_db(t)
    assert meta["owner_id"] == owner_uri(takeover_session_id)
    assert "Previous holder: `test holder`." in cv["work_log_md"]


def test_release_clears_sqlite_owner_and_renders_last_operation(tickets_root):
    t = make_ticket()
    assert run([
        "ticket", str(t), "release",
        *owner_args(), "--owner-label", "test holder",
    ]) == 0
    meta, cv = read_db(t)
    assert meta["owner_id"] is None
    assert meta["lifecycle_state"] == "BACKLOG"
    assert meta["owner_last_action"] == "release"
    assert "Released ticket by `test holder`" in cv["work_log_md"]
    md = (Path(t) / "TICKET.md").read_text()
    assert "Lifecycle: BACKLOG" in md
    assert "Owner: release by test holder at " in md


def test_claim_moves_backlog_ticket_to_active(tickets_root, capsys):
    assert run([
        "ticket", "new",
        "--topic", "claim-later",
        "--goal", "Claim later",
        "--backlog",
    ]) == 0
    t = Path(capsys.readouterr().out.strip())

    assert run([
        "ticket", str(t), "claim",
        *owner_args(), "--owner-label", "test holder",
    ]) == 0

    meta, cv = read_db(t)
    assert meta["lifecycle_state"] == "ACTIVE"
    assert meta["owner_id"] == DEFAULT_OWNER_URI
    assert meta["owner_last_action"] == "claim"
    assert "Claimed ticket by `test holder`" in cv["work_log_md"]
    md = (Path(t) / "TICKET.md").read_text()
    assert "Lifecycle: ACTIVE" in md
    assert "Owner: claim by test holder at " in md


def test_new_claim_release_can_infer_owner_from_environment(tickets_root, monkeypatch):
    env_session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    create_codex_session(Path(os.environ["CODEX_JSONL_ROOT"]), env_session_id)
    monkeypatch.setenv("CODEX_THREAD_ID", env_session_id)
    monkeypatch.setenv("CODEX_CI", "1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)

    from ticket_cli.ticket_new import create_ticket

    t = create_ticket("env-owner", goal="env owner goal")
    meta, _ = read_db(t)
    assert meta["owner_id"] == owner_uri(env_session_id)

    assert run(["ticket", str(t), "release"]) == 0
    meta, _ = read_db(t)
    assert meta["owner_id"] is None
    assert meta["lifecycle_state"] == "BACKLOG"

    assert run(["ticket", str(t), "claim"]) == 0
    meta, _ = read_db(t)
    assert meta["owner_id"] == owner_uri(env_session_id)
    assert meta["lifecycle_state"] == "ACTIVE"


def test_infer_holder_requires_args_or_provider_env(monkeypatch):
    from ticket_cli.identity import infer_holder

    for key in ("ATICKET_HOLDER_LABEL", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDECODE"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(SystemExit, match="could not infer"):
        infer_holder()


def test_infer_holder_ignores_owner_xurl_env(monkeypatch):
    from ticket_cli.identity import infer_holder

    monkeypatch.setenv("ATICKET_OWNER_XURL", DEFAULT_OWNER_URI)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    with pytest.raises(SystemExit, match="could not infer"):
        infer_holder()


def test_infer_holder_accepts_existing_agent_session(monkeypatch, tmp_path):
    from ticket_cli.identity import infer_holder

    session_id = "22222222-2222-4222-8222-222222222222"
    create_codex_session(tmp_path / "codex-sessions", session_id)
    monkeypatch.setenv("CODEX_JSONL_ROOT", str(tmp_path / "codex-sessions"))
    for key in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ATICKET_HOLDER_LABEL", "explicit label")

    holder = infer_holder(agent_type="codex", session_id=session_id)

    assert holder.holder_id == owner_uri(session_id)
    assert holder.holder_label == "explicit label"
    assert holder.holder_id_source == "arg:--agent-type/--session-id"


def test_infer_holder_uses_codex_thread_env(monkeypatch, tmp_path):
    from ticket_cli.identity import infer_holder

    session_id = "66666666-6666-4666-8666-666666666666"
    create_codex_session(tmp_path / "codex-sessions", session_id)
    monkeypatch.setenv("CODEX_JSONL_ROOT", str(tmp_path / "codex-sessions"))
    monkeypatch.setenv("CODEX_THREAD_ID", session_id)
    monkeypatch.setenv("CODEX_CI", "1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)

    holder = infer_holder()

    assert holder.holder_id == owner_uri(session_id)
    assert holder.holder_label == "codex (66666666)"
    assert holder.holder_id_source == "env:CODEX_THREAD_ID"


def test_infer_holder_uses_claude_session_env(monkeypatch, tmp_path):
    from ticket_cli.identity import infer_holder

    session_id = "77777777-7777-4777-8777-777777777777"
    create_claude_session(tmp_path / "claude-projects", session_id)
    monkeypatch.setenv("CLAUDE_JSONL_ROOT", str(tmp_path / "claude-projects"))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_CI", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
    monkeypatch.setenv("CLAUDECODE", "1")

    holder = infer_holder()

    assert holder.holder_id == owner_uri(session_id, provider="claude")
    assert holder.holder_label == "claude (77777777)"
    assert holder.holder_id_source == "env:CLAUDE_CODE_SESSION_ID"


def test_infer_holder_prefers_current_runtime_marker_when_both_envs_exist(monkeypatch, tmp_path):
    from ticket_cli import identity

    codex_session_id = "88888888-8888-4888-8888-888888888888"
    claude_session_id = "99999999-9999-4999-8999-999999999999"
    create_codex_session(tmp_path / "codex-sessions", codex_session_id)
    create_claude_session(tmp_path / "claude-projects", claude_session_id)
    monkeypatch.setenv("CODEX_JSONL_ROOT", str(tmp_path / "codex-sessions"))
    monkeypatch.setenv("CLAUDE_JSONL_ROOT", str(tmp_path / "claude-projects"))
    monkeypatch.setenv("CODEX_THREAD_ID", codex_session_id)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", claude_session_id)
    monkeypatch.setenv("CODEX_CI", "1")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(identity, "_nearest_agent_provider", lambda: "claude")

    holder = identity.infer_holder()

    assert holder.holder_id == owner_uri(claude_session_id, provider="claude")


def test_infer_holder_rejects_partial_explicit_identity(monkeypatch):
    from ticket_cli.identity import infer_holder

    monkeypatch.setenv("CODEX_THREAD_ID", DEFAULT_OWNER_SESSION_ID)

    with pytest.raises(SystemExit, match="pass both --agent-type"):
        infer_holder(agent_type="codex")


def test_infer_holder_accepts_existing_claude_session(monkeypatch, tmp_path):
    from ticket_cli.identity import infer_holder

    session_id = "33333333-3333-4333-8333-333333333333"
    create_claude_session(tmp_path / "claude-projects", session_id)
    monkeypatch.setenv("CLAUDE_JSONL_ROOT", str(tmp_path / "claude-projects"))

    holder = infer_holder(agent_type="claude", session_id=session_id)

    assert holder.holder_id == owner_uri(session_id, provider="claude")
    assert holder.holder_label == "claude (33333333)"


def test_infer_holder_rejects_claude_session_id_found_only_in_jsonl_body(monkeypatch, tmp_path):
    from ticket_cli.identity import infer_holder

    session_id = "55555555-5555-4555-8555-555555555555"
    project_dir = tmp_path / "claude-projects" / "-code-tsshi-aticket-cli"
    project_dir.mkdir(parents=True)
    (project_dir / "not-the-session-id.jsonl").write_text(
        f'{{"sessionId":"{session_id}"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_JSONL_ROOT", str(tmp_path / "claude-projects"))

    with pytest.raises(SystemExit, match="does not resolve to a local claude session"):
        infer_holder(agent_type="claude", session_id=session_id)


def test_infer_holder_rejects_missing_local_agent_session(monkeypatch, tmp_path):
    from ticket_cli.identity import infer_holder

    monkeypatch.setenv("CODEX_JSONL_ROOT", str(tmp_path / "codex-sessions"))

    with pytest.raises(SystemExit, match="does not resolve to a local codex session"):
        infer_holder(agent_type="codex", session_id="44444444-4444-4444-8444-444444444444")


def test_infer_holder_rejects_invalid_agent_type():
    from ticket_cli.identity import infer_holder

    with pytest.raises(SystemExit, match="--agent-type must be codex or claude"):
        infer_holder(agent_type="local", session_id=DEFAULT_OWNER_SESSION_ID)


def test_infer_holder_rejects_invalid_session_id():
    from ticket_cli.identity import infer_holder

    with pytest.raises(SystemExit, match="--session-id must be a UUID"):
        infer_holder(agent_type="codex", session_id="not-a-uuid")


def test_append_atomicity_no_lost_updates(tickets_root):
    t = make_ticket()
    for i in range(8):
        assert run(["ticket", str(t), "log", f"entry {i}"]) == 0
    _, cv = read_db(t)
    bullets = [ln for ln in cv["work_log_md"].splitlines() if ln.startswith("- ")]
    assert len(bullets) == 9
    assert all(f"entry {i}" in cv["work_log_md"] for i in range(8))


def test_lifecycle_archive_sets_timestamp(tickets_root):
    t = make_ticket()
    assert run(["ticket", str(t), "archive"]) == 0
    meta, _ = read_db(t)
    assert meta["lifecycle_state"] == "ARCHIVED"
    assert meta["archived_at"]


def test_archive_large_ticket_requires_agent_confirmation(tickets_root, monkeypatch):
    from ticket_cli import lifecycle

    t = make_ticket()
    monkeypatch.setattr(lifecycle, "_ticket_dir_size_bytes", lambda ticket_dir: 11 * 1024 * 1024)

    with pytest.raises(SystemExit, match="--agent-confirm-archive-large-dir"):
        run(["ticket", str(t), "archive"])

    meta, _ = read_db(t)
    assert meta["lifecycle_state"] == "ACTIVE"

    assert run(["ticket", str(t), "archive", "--agent-confirm-archive-large-dir"]) == 0
    meta, _ = read_db(t)
    assert meta["lifecycle_state"] == "ARCHIVED"


def test_archive_large_already_archived_ticket_is_idempotent_without_confirmation(tickets_root, monkeypatch):
    from ticket_cli import lifecycle

    t = make_ticket()
    assert run(["ticket", str(t), "archive"]) == 0
    monkeypatch.setattr(lifecycle, "_ticket_dir_size_bytes", lambda ticket_dir: 101 * 1024 * 1024)

    assert run(["ticket", str(t), "archive"]) == 0
    meta, _ = read_db(t)
    assert meta["lifecycle_state"] == "ARCHIVED"


def test_archive_large_ticket_accepts_legacy_agent_flag_typo(tickets_root, monkeypatch):
    from ticket_cli import lifecycle

    t = make_ticket()
    monkeypatch.setattr(lifecycle, "_ticket_dir_size_bytes", lambda ticket_dir: 11 * 1024 * 1024)

    assert run(["ticket", str(t), "archive", "--agnet-confirm-archive-large-dir"]) == 0
    meta, _ = read_db(t)
    assert meta["lifecycle_state"] == "ARCHIVED"


def test_archive_huge_ticket_requires_human_confirmation(tickets_root, monkeypatch):
    from ticket_cli import lifecycle

    t = make_ticket()
    monkeypatch.setattr(lifecycle, "_ticket_dir_size_bytes", lambda ticket_dir: 101 * 1024 * 1024)

    with pytest.raises(SystemExit, match="--human-confirm-archive-large-dir"):
        run(["ticket", str(t), "archive", "--agent-confirm-archive-large-dir"])

    meta, _ = read_db(t)
    assert meta["lifecycle_state"] == "ACTIVE"

    assert run(["ticket", str(t), "archive", "--human-confirm-archive-large-dir"]) == 0
    meta, _ = read_db(t)
    assert meta["lifecycle_state"] == "ARCHIVED"


def test_archive_fails_closed_when_size_scan_fails(tickets_root, monkeypatch):
    from ticket_cli import lifecycle

    t = make_ticket()

    def fail_scandir(path):
        raise OSError("scan failed")

    monkeypatch.setattr(lifecycle.os, "scandir", fail_scandir)

    with pytest.raises(SystemExit, match="cannot measure ticket directory size"):
        run(["ticket", str(t), "archive"])

    meta, _ = read_db(t)
    assert meta["lifecycle_state"] == "ACTIVE"


def test_items_are_append_only(tickets_root):
    import json
    t = make_ticket()

    assert run(["ticket", str(t), "add-item", "file:///code/tsshi/my-repo"]) == 0
    assert run(["ticket", str(t), "add-item", "https://github.example.com/org/foo/pull/1"]) == 0
    _, cv = read_db(t)
    items = json.loads(cv["items"])
    assert items == ["file:///code/tsshi/my-repo", "https://github.example.com/org/foo/pull/1"]

    assert run(["ticket", str(t), "add-item", "https://docs.example.com/page/123"]) == 0
    _, cv = read_db(t)
    items = json.loads(cv["items"])
    assert len(items) == 3
    assert "https://docs.example.com/page/123" in items

    assert run(["ticket", str(t), "add-item", "https://docs.example.com/page/123"]) == 0
    _, cv = read_db(t)
    assert len(json.loads(cv["items"])) == 3

    assert run(["ticket", str(t), "add-item", "agents://codex/review-thread"]) == 0
    _, cv = read_db(t)
    items = json.loads(cv["items"])
    assert len(items) == 4

    md = (Path(t) / "TICKET.md").read_text()
    assert "- file:///code/tsshi/my-repo" in md
    assert "- https://github.example.com/org/foo/pull/1" in md
    assert "- https://docs.example.com/page/123" in md
    assert "- agents://codex/review-thread" in md


def test_archive_makes_ticket_immutable(tickets_root):
    t = make_ticket()
    assert run(["ticket", str(t), "archive"]) == 0
    with pytest.raises(SystemExit, match="ARCHIVED"):
        run(["ticket", str(t), "log", "too late"])
    with pytest.raises(SystemExit, match="ARCHIVED"):
        run(["ticket", str(t), "goal", "too late"])
    with pytest.raises(SystemExit, match="ARCHIVED"):
        run(["ticket", str(t), "claim", *owner_args(), "--force"])
