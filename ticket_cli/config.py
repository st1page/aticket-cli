"""Runtime configuration for aticket-cli."""
from __future__ import annotations

import os
import sys
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any

CONFIG_PATH_ENV = "ATICKET_CONFIG"
CONFIG_PATH_DEFAULT = "~/.config/aticket-cli/config.toml"

AGENT_TICKETS_ROOT_DEFAULT = "/code/tsshi/agent-tickets"
ARCHIVE_AGENT_CONFIRM_LARGE_DIR_MIB_DEFAULT = 10
ARCHIVE_HUMAN_CONFIRM_LARGE_DIR_MIB_DEFAULT = 100
CODEX_JSONL_ROOT_DEFAULT = "~/.codex/sessions"
CLAUDE_JSONL_ROOT_DEFAULT = "~/.claude/projects"


def config_path() -> Path:
    raw = os.environ.get(CONFIG_PATH_ENV, CONFIG_PATH_DEFAULT)
    return Path(raw).expanduser().resolve()


def default_config_text() -> str:
    try:
        return resources.files("ticket_cli").joinpath("templates/default-config.toml").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return (
            "[tickets]\n"
            f'root = "{AGENT_TICKETS_ROOT_DEFAULT}"\n'
            "\n"
            "[archive]\n"
            f"agent_confirm_large_dir_mib = {ARCHIVE_AGENT_CONFIRM_LARGE_DIR_MIB_DEFAULT}\n"
            f"human_confirm_large_dir_mib = {ARCHIVE_HUMAN_CONFIRM_LARGE_DIR_MIB_DEFAULT}\n"
        )


def ensure_config_file_for_command() -> Path:
    """Ensure non-help CLI commands have a config file before dispatch.

    Explicit ATICKET_CONFIG is never created implicitly: a missing explicit path
    usually means the caller pointed at the wrong config. The default path is
    bootstrapped so a normal package install can run without a fragile pip
    post-install hook that writes into a user's home directory.
    """
    path = config_path()
    if path.is_file():
        load_config()
        return path
    if os.environ.get(CONFIG_PATH_ENV):
        raise SystemExit(f"aticket config file not found: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as fh:
            fh.write(default_config_text())
    except FileExistsError:
        if path.is_file():
            return path
        raise SystemExit(f"aticket config path exists but is not a file: {path}")
    except OSError as exc:
        raise SystemExit(f"cannot create aticket config {path}: {exc}") from exc
    sys.stderr.write(
        f"created aticket config directory: {path.parent}\n"
        f"created aticket config file: {path}\n"
        "Review and edit [tickets].root before relying on this installation.\n"
    )
    load_config()
    return path


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        raise SystemExit(f"aticket config file not found: {path}")
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise SystemExit(f"cannot read aticket config {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"invalid aticket config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"invalid aticket config {path}: top-level value must be a table")
    return data


def _section(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    raw = cfg.get(name, {})
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SystemExit(f"invalid aticket config: [{name}] must be a table")
    return raw


def _number(section: dict[str, Any], key: str, default: int | float) -> int | float:
    raw = section.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SystemExit(f"invalid aticket config: {key} must be a number")
    if raw < 0:
        raise SystemExit(f"invalid aticket config: {key} must be >= 0")
    return raw


def _mib_to_bytes(value: int | float) -> int:
    return int(float(value) * 1024 * 1024)


def configured_tickets_root(raw: str | None = None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    env_root = os.environ.get("AGENT_TICKETS_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    tickets = _section(load_config(), "tickets")
    config_root = tickets.get("root", AGENT_TICKETS_ROOT_DEFAULT)
    if not isinstance(config_root, str) or not config_root.strip():
        raise SystemExit("invalid aticket config: tickets.root must be a non-empty string")
    return Path(config_root).expanduser().resolve()


def archive_large_dir_thresholds_bytes() -> tuple[int, int]:
    archive = _section(load_config(), "archive")
    agent_mib = _number(
        archive,
        "agent_confirm_large_dir_mib",
        ARCHIVE_AGENT_CONFIRM_LARGE_DIR_MIB_DEFAULT,
    )
    human_mib = _number(
        archive,
        "human_confirm_large_dir_mib",
        ARCHIVE_HUMAN_CONFIRM_LARGE_DIR_MIB_DEFAULT,
    )
    agent_bytes = _mib_to_bytes(agent_mib)
    human_bytes = _mib_to_bytes(human_mib)
    if human_bytes < agent_bytes:
        raise SystemExit(
            "invalid aticket config: archive.human_confirm_large_dir_mib "
            "must be >= archive.agent_confirm_large_dir_mib"
        )
    return agent_bytes, human_bytes


def configured_session_root(provider: str) -> Path:
    if provider == "codex":
        env_key = "CODEX_JSONL_ROOT"
        config_key = "codex_jsonl_root"
        default = CODEX_JSONL_ROOT_DEFAULT
    elif provider == "claude":
        env_key = "CLAUDE_JSONL_ROOT"
        config_key = "claude_jsonl_root"
        default = CLAUDE_JSONL_ROOT_DEFAULT
    else:
        raise SystemExit(f"unsupported aticket owner xurl provider: {provider}")

    env_root = os.environ.get(env_key)
    if env_root:
        return Path(env_root).expanduser()
    identity = _section(load_config(), "identity")
    config_root = identity.get(config_key, default)
    if not isinstance(config_root, str) or not config_root.strip():
        raise SystemExit(f"invalid aticket config: identity.{config_key} must be a non-empty string")
    return Path(config_root).expanduser()
