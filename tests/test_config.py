import pytest

from conftest import create_codex_session, make_ticket, owner_uri, read_db, run
from ticket_cli.config import configured_session_root
from ticket_cli.identity import infer_holder
from ticket_cli.paths import agent_tickets_root


def test_agent_tickets_root_can_come_from_config(tmp_path, monkeypatch):
    configured_root = tmp_path / "configured-agent-tickets"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[tickets]\nroot = "{configured_root.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("AGENT_TICKETS_ROOT", raising=False)
    monkeypatch.setenv("ATICKET_CONFIG", str(config))

    assert agent_tickets_root() == configured_root.resolve()


def test_agent_tickets_root_env_overrides_config(tmp_path, monkeypatch):
    configured_root = tmp_path / "configured-agent-tickets"
    env_root = tmp_path / "env-agent-tickets"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[tickets]\nroot = "{configured_root.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_TICKETS_ROOT", str(env_root))
    monkeypatch.setenv("ATICKET_CONFIG", str(config))

    assert agent_tickets_root() == env_root.resolve()


def test_explicit_missing_config_file_fails_closed(tmp_path, monkeypatch):
    missing_config = tmp_path / "missing.toml"
    monkeypatch.delenv("AGENT_TICKETS_ROOT", raising=False)
    monkeypatch.setenv("ATICKET_CONFIG", str(missing_config))

    with pytest.raises(SystemExit, match="aticket config file not found"):
        agent_tickets_root()


def test_non_help_command_bootstraps_default_config(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    tickets_root = tmp_path / "agent-tickets"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ATICKET_CONFIG", raising=False)
    monkeypatch.setenv("AGENT_TICKETS_ROOT", str(tickets_root))
    monkeypatch.setenv("CODEX_JSONL_ROOT", str(tmp_path / "codex-sessions"))
    monkeypatch.setenv("CLAUDE_JSONL_ROOT", str(tmp_path / "claude-projects"))

    assert run(["tickets", "search", "--query", "nothing"]) == 0

    config = home / ".config" / "aticket-cli" / "config.toml"
    assert config.is_file()
    text = config.read_text(encoding="utf-8")
    assert "[tickets]" in text
    assert "root = " in text
    assert "agent_confirm_large_dir_mib = 10" in text
    assert "codex_jsonl_root" not in text
    assert "claude_jsonl_root" not in text
    err = capsys.readouterr().err
    assert f"created aticket config directory: {config.parent}" in err
    assert f"created aticket config file: {config}" in err
    assert "Review and edit [tickets].root" in err
    assert "codex_jsonl_root" not in err
    assert "claude_jsonl_root" not in err


def test_help_does_not_bootstrap_default_config(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ATICKET_CONFIG", raising=False)

    with pytest.raises(SystemExit) as exc:
        run(["--help"])

    assert exc.value.code == 0
    assert "usage: aticket-cli <resource>" in capsys.readouterr().out
    assert not (home / ".config" / "aticket-cli" / "config.toml").exists()


def test_explicit_missing_config_blocks_valid_cli_command(tmp_path, monkeypatch):
    missing_config = tmp_path / "missing.toml"
    monkeypatch.setenv("ATICKET_CONFIG", str(missing_config))
    monkeypatch.setenv("AGENT_TICKETS_ROOT", str(tmp_path / "agent-tickets"))

    with pytest.raises(SystemExit, match="aticket config file not found"):
        run(["tickets", "search", "--query", "nothing"])


def test_invalid_explicit_config_blocks_valid_cli_command_even_with_env_root(tmp_path, monkeypatch):
    config = tmp_path / "bad.toml"
    config.write_text("[tickets\n", encoding="utf-8")
    monkeypatch.setenv("ATICKET_CONFIG", str(config))
    monkeypatch.setenv("AGENT_TICKETS_ROOT", str(tmp_path / "agent-tickets"))

    with pytest.raises(SystemExit, match="invalid aticket config"):
        run(["tickets", "search", "--query", "nothing"])


def test_identity_session_roots_can_come_from_config(tmp_path, monkeypatch):
    codex_root = tmp_path / "configured-codex-sessions"
    claude_root = tmp_path / "configured-claude-projects"
    config = tmp_path / "config.toml"
    config.write_text(
        "[identity]\n"
        f'codex_jsonl_root = "{codex_root.as_posix()}"\n'
        f'claude_jsonl_root = "{claude_root.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("CODEX_JSONL_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_JSONL_ROOT", raising=False)
    monkeypatch.setenv("ATICKET_CONFIG", str(config))

    assert configured_session_root("codex") == codex_root
    assert configured_session_root("claude") == claude_root


def test_identity_session_root_env_overrides_config(tmp_path, monkeypatch):
    configured_root = tmp_path / "configured-codex-sessions"
    env_root = tmp_path / "env-codex-sessions"
    config = tmp_path / "config.toml"
    config.write_text(
        "[identity]\n"
        f'codex_jsonl_root = "{configured_root.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_JSONL_ROOT", str(env_root))
    monkeypatch.setenv("ATICKET_CONFIG", str(config))

    assert configured_session_root("codex") == env_root


def test_infer_holder_validates_session_from_config_root(tmp_path, monkeypatch):
    session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    codex_root = tmp_path / "configured-codex-sessions"
    create_codex_session(codex_root, session_id)
    config = tmp_path / "config.toml"
    config.write_text(
        "[identity]\n"
        f'codex_jsonl_root = "{codex_root.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("CODEX_JSONL_ROOT", raising=False)
    monkeypatch.setenv("ATICKET_CONFIG", str(config))

    holder = infer_holder(agent_type="codex", session_id=session_id)

    assert holder.holder_id == owner_uri(session_id)


def test_archive_thresholds_can_come_from_config(tickets_root, tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        "[archive]\n"
        "agent_confirm_large_dir_mib = 0\n"
        "human_confirm_large_dir_mib = 999\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ATICKET_CONFIG", str(config))

    t = make_ticket()
    result = read_db(t)[0]
    assert result["lifecycle_state"] == "ACTIVE"

    with pytest.raises(SystemExit, match="--agent-confirm-archive-large-dir"):
        run(["ticket", str(t), "archive"])

    assert run(["ticket", str(t), "archive", "--agent-confirm-archive-large-dir"]) == 0
    meta, _ = read_db(t)
    assert meta["lifecycle_state"] == "ARCHIVED"
