"""Global search index: ticket indexing and rebuilds."""
import sqlite3
from pathlib import Path

import pytest

from conftest import make_ticket, owner_args, run, seed_legacy_artifacts


def test_search_finds_ticket_text_and_cjk(tickets_root, capsys):
    t = make_ticket("search-demo")
    assert run(["ticket", str(t), "goal", "支持中文分词 and tmux-pane PR#386"]) == 0

    capsys.readouterr()
    assert run(["tickets", "search", "--query", "中文分词"]) == 0
    out = capsys.readouterr().out
    assert str(t) in out
    assert "search-demo" in out.lower()

    capsys.readouterr()
    assert run(["tickets", "search", "--query", "pr386"]) == 0
    assert str(t) in capsys.readouterr().out


def test_search_finds_must_remember_text(tickets_root, capsys):
    t = make_ticket("remember-search")
    assert run(["ticket", str(t), "remember", "Preflight sentinel keepaliveomega"]) == 0

    capsys.readouterr()
    assert run(["tickets", "search", "--query", "keepaliveomega"]) == 0
    assert str(t) in capsys.readouterr().out


def test_search_reindex_finds_legacy_artifacts_text(tickets_root, capsys):
    t = make_ticket("legacy-artifact-search")
    seed_legacy_artifacts(t, "Legacy artifact sentinel legacyartifactomega")

    capsys.readouterr()
    assert run(["tickets", "search", "--reindex"]) == 0
    assert "reindexed" in capsys.readouterr().out

    assert run(["tickets", "search", "--query", "legacyartifactomega"]) == 0
    assert str(t) in capsys.readouterr().out


def test_search_requires_query_or_reindex(capsys):
    with pytest.raises(SystemExit) as exc:
        run(["tickets", "search"])
    assert exc.value.code == 2
    assert "one of the arguments --query --reindex is required" in capsys.readouterr().err


def test_search_reindex_rebuilds_missing_db(tickets_root, capsys):
    t = make_ticket("reindex-demo")
    assert run(["ticket", str(t), "goal", "backlog triage keyword"]) == 0
    search_db = Path(tickets_root) / "search.sqlite3"
    assert search_db.exists()
    search_db.unlink()

    capsys.readouterr()
    assert run(["tickets", "search", "--query", "backlog"]) == 0
    assert capsys.readouterr().out.strip() == ""

    capsys.readouterr()
    assert run(["tickets", "search", "--reindex"]) == 0
    assert "reindexed" in capsys.readouterr().out

    capsys.readouterr()
    assert run(["tickets", "search", "--query", "backlog"]) == 0
    assert str(t) in capsys.readouterr().out


def test_search_can_filter_backlog_tickets(tickets_root, capsys):
    assert run([
        "ticket", "new",
        "--topic", "backlog-filter",
        "--goal", "backlog searchablealpha",
        "--backlog",
    ]) == 0
    backlog = Path(capsys.readouterr().out.strip())
    active = make_ticket("active-filter")
    assert run(["ticket", str(active), "goal", "backlog searchablealpha active"]) == 0

    capsys.readouterr()
    assert run([
        "tickets", "search",
        "--query", "searchablealpha",
        "--lifecycle-state", "BACKLOG",
    ]) == 0
    out = capsys.readouterr().out
    assert str(backlog) in out
    assert str(active) not in out


def test_search_can_filter_to_direct_ticket_arguments(tickets_root, capsys):
    included = make_ticket("ticket-direct-included")
    excluded = make_ticket("ticket-direct-excluded")
    assert run(["ticket", str(included), "goal", "directsearchalpha included"]) == 0
    assert run(["ticket", str(excluded), "goal", "directsearchalpha excluded"]) == 0

    capsys.readouterr()
    assert run([
        "tickets", "search",
        "--query", "directsearchalpha",
        "--ticket", included.name,
    ]) == 0
    out = capsys.readouterr().out
    assert str(included) in out
    assert str(excluded) not in out


def test_search_ticket_accepts_absolute_path_file_uri_and_duplicates(tickets_root, capsys):
    included = make_ticket("ticket-direct-uri-included")
    excluded = make_ticket("ticket-direct-uri-excluded")
    assert run(["ticket", str(included), "goal", "urisearchalpha included"]) == 0
    assert run(["ticket", str(excluded), "goal", "urisearchalpha excluded"]) == 0

    capsys.readouterr()
    assert run([
        "tickets", "search",
        "--query", "urisearchalpha",
        "--ticket", str(included),
        "--ticket", f"file://{included}",
    ]) == 0
    out = capsys.readouterr().out
    assert str(included) in out
    assert str(excluded) not in out


def test_search_ticket_rejects_non_ticket_entries(tickets_root, tmp_path):
    with pytest.raises(SystemExit) as exc:
        run(["tickets", "search", "--query", "anything", "--ticket", str(tmp_path / "not-a-ticket")])
    assert "--ticket is not a ticket directory" in str(exc.value)


def test_safe_upsert_ticket_reports_broken_search_index(monkeypatch, tmp_path):
    from ticket_cli import search_index

    def boom(ticket_dir):
        raise RuntimeError("sentinel upsert failure")

    monkeypatch.setattr(search_index, "upsert_ticket", boom)

    with pytest.raises(SystemExit) as exc:
        search_index.safe_upsert_ticket(tmp_path / "ticket")

    msg = str(exc.value)
    assert "search index update failed" in msg
    assert "sentinel upsert failure" in msg
    assert "Agent: notify the user that the search index is broken" in msg
    assert "aticket-cli tickets search --reindex" in msg


def test_safe_delete_ticket_reports_broken_search_index(monkeypatch, tmp_path):
    from ticket_cli import search_index

    def boom(ticket_dir):
        raise RuntimeError("sentinel delete failure")

    monkeypatch.setattr(search_index, "delete_ticket", boom)

    with pytest.raises(SystemExit) as exc:
        search_index.safe_delete_ticket(tmp_path / "ticket")

    msg = str(exc.value)
    assert "search index delete failed" in msg
    assert "sentinel delete failure" in msg
    assert "Agent: notify the user that the search index is broken" in msg
    assert "aticket-cli tickets search --reindex" in msg


def test_search_ignores_stale_non_ticket_rows(tickets_root, capsys):
    make_ticket("stale-row")
    search_db = Path(tickets_root) / "search.sqlite3"
    conn = sqlite3.connect(search_db)
    try:
        conn.execute(
            """INSERT INTO docs_fts
               (kind, path, title, lifecycle_state, updated_at, body, tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("task", "/old/task", "old task", "ACTIVE", "now", "staletask", "staletask"),
        )
        conn.commit()
    finally:
        conn.close()

    capsys.readouterr()
    assert run(["tickets", "search", "--query", "staletask"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_search_finds_unread_and_checked_notice_text(tickets_root, capsys):
    t = make_ticket("notice-search")
    unique = "noticeuniquealpha"
    evidence = "noticeevidencealpha"
    assert run([
        "message", "send",
        "--ticket", str(t),
        f"{unique} please inspect",
        "--with", f"file:///tmp/{evidence}",
        *owner_args(),
    ]) == 0

    capsys.readouterr()
    assert run(["tickets", "search", "--query", unique]) == 0
    assert str(t) in capsys.readouterr().out

    search_db = Path(tickets_root) / "search.sqlite3"
    search_db.unlink()
    capsys.readouterr()
    assert run(["tickets", "search", "--reindex"]) == 0
    assert "reindexed" in capsys.readouterr().out

    capsys.readouterr()
    assert run(["tickets", "search", "--query", evidence]) == 0
    assert str(t) in capsys.readouterr().out

    assert run([
        "ticket", str(t), "message", "checked",
        "--until-id", "1",
        *owner_args(),
    ]) == 0
    capsys.readouterr()
    assert run(["tickets", "search", "--query", unique]) == 0
    assert str(t) in capsys.readouterr().out


def test_search_finds_archived_historical_message_text(tickets_root, capsys):
    t = make_ticket("archived-message-search")
    unique = "archivedmessageuniquealpha"

    assert run(["ticket", str(t), "archive"]) == 0
    capsys.readouterr()
    assert run([
        "message", "send",
        "--ticket", str(t),
        f"{unique} preserved after archive",
        "--allow-archived",
        *owner_args(),
    ]) == 0
    capsys.readouterr()

    assert run(["tickets", "search", "--query", unique]) == 0
    assert str(t) in capsys.readouterr().out

    search_db = Path(tickets_root) / "search.sqlite3"
    search_db.unlink()
    assert run(["tickets", "search", "--reindex"]) == 0
    capsys.readouterr()

    assert run(["tickets", "search", "--query", unique]) == 0
    assert str(t) in capsys.readouterr().out
