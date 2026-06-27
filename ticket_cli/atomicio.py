"""Atomic file writes, json helpers, and an atomic lock-dir primitive."""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import time
from pathlib import Path


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{secrets.token_hex(4)}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json_atomic(path: Path, data: dict) -> None:
    write_text_atomic(
        path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def load_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


@contextlib.contextmanager
def dir_lock(lock_path: Path, *, attempts: int = 50, sleep: float = 0.1):
    """Atomic mutual exclusion via mkdir (atomic on POSIX)."""
    lock_dir = Path(str(lock_path) + ".d")
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    for _ in range(attempts):
        try:
            lock_dir.mkdir()
            acquired = True
            break
        except FileExistsError:
            time.sleep(sleep)
    if not acquired:
        raise SystemExit(f"failed to acquire lock: {lock_dir}")
    try:
        yield lock_dir
    finally:
        with contextlib.suppress(OSError):
            lock_dir.rmdir()
