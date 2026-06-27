"""Root-level global search index for tickets.

The primary ticket truth remains each ticket's sqlite DB. This module maintains
a derived FTS5 index under AGENT_TICKETS_ROOT so search stays global and fast
without changing the ticket source-of-truth store.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlparse

from .paths import agent_tickets_root, canonical_tickets_dir

SEARCH_DB_FILENAME = "search.sqlite3"
_CJK_CLASS = r"\u3400-\u4dbf\u4e00-\u9fff"

_SCHEMA_DDL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
  kind UNINDEXED,
  path UNINDEXED,
  title UNINDEXED,
  lifecycle_state UNINDEXED,
  updated_at UNINDEXED,
  body UNINDEXED,
  tokens,
  tokenize='unicode61 remove_diacritics 2'
);
"""


def search_db_path(root: Path) -> Path:
    return root / SEARCH_DB_FILENAME


def _open_search_db(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(search_db_path(root)))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_DDL)
    conn.commit()
    return conn


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def _add_token(tokens: list[str], seen: set[str], token: str) -> None:
    token = token.strip()
    if not token:
        return
    if len(token) == 1 and not token.isdigit():
        return
    if token in seen:
        return
    seen.add(token)
    tokens.append(token)


def tokenize_text(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    normalized = _normalized(text)
    for chunk in re.split(r"\s+", normalized):
        if not chunk:
            continue
        has_cjk = re.search(fr"[{_CJK_CLASS}]", chunk) is not None
        collapsed = re.sub(fr"[^0-9a-z{_CJK_CLASS}]+", "", chunk)
        if not has_cjk:
            _add_token(tokens, seen, collapsed)
        for match in re.finditer(fr"[a-z]+|\d+|[{_CJK_CLASS}]+", chunk):
            part = match.group(0)
            if re.fullmatch(fr"[{_CJK_CLASS}]+", part):
                for gram_size in (2, 3):
                    if len(part) < gram_size:
                        continue
                    for i in range(len(part) - gram_size + 1):
                        _add_token(tokens, seen, part[i : i + gram_size])
            else:
                _add_token(tokens, seen, part)
    return tokens


def _search_text(*parts: str) -> str:
    return " ".join(tokenize_text("\n".join(part for part in parts if part)))


def _fts_query(text: str) -> str:
    tokens = tokenize_text(text)
    if not tokens:
        raise SystemExit("search query produced no searchable tokens")
    return " ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _snippet(body: str, query: str, *, width: int = 90) -> str:
    clean = " ".join((body or "").split())
    if not clean:
        return ""
    lower_body = clean.lower()
    lower_query = _normalized(query)
    idx = lower_body.find(lower_query)
    if idx < 0:
        for token in tokenize_text(query):
            idx = lower_body.find(token)
            if idx >= 0:
                break
    if idx < 0:
        return clean[:width]
    start = max(0, idx - width // 3)
    end = min(len(clean), idx + width)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(clean) else ""
    return f"{prefix}{clean[start:end]}{suffix}"


def _root_from_ticket_dir(ticket_dir: Path) -> Path:
    return ticket_dir.parent.parent


def _path_from_ticket_arg(raw: str, *, root: Path) -> Path | None:
    entry = raw.strip()
    if not entry:
        return None
    if entry.startswith("file://"):
        parsed = urlparse(entry)
        if parsed.netloc and parsed.netloc != "localhost":
            raise SystemExit(f"ticket file URI must be local: {entry}")
        entry = unquote(parsed.path)
    path = Path(entry).expanduser()
    if path.is_absolute():
        return path.resolve()
    if len(path.parts) == 1:
        return (canonical_tickets_dir(root) / path).resolve()
    return path.resolve()


def _append_scoped_path(scoped: list[str], seen: set[str], path: Path, *, source: str) -> None:
    if not (path / "state" / "ticket.sqlite3").exists():
        raise SystemExit(f"{source} is not a ticket directory: {path}")
    normalized = str(path)
    if normalized in seen:
        return
    seen.add(normalized)
    scoped.append(normalized)


def _scoped_ticket_paths(direct_tickets: list[str], *, root: Path) -> list[str] | None:
    if not direct_tickets:
        return None

    scoped: list[str] = []
    seen: set[str] = set()
    for raw_ticket in direct_tickets:
        path = _path_from_ticket_arg(raw_ticket, root=root)
        if path is None:
            continue
        _append_scoped_path(scoped, seen, path, source="--ticket")
    return scoped


def _flatten_json_list(val: str | None) -> str:
    if not val:
        return ""
    try:
        data = json.loads(val)
        if isinstance(data, list):
            return " ".join(str(item) for item in data)
        return str(val)
    except Exception:
        return str(val)


def _notice_search_text(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """SELECT message, from_ticket, with_items_json
           FROM notices
           WHERE (checked_at IS NULL AND archived_delivery = 0)
              OR archived_delivery = 1
           ORDER BY id ASC"""
    ).fetchall()
    parts: list[str] = []
    for row in rows:
        parts.append(str(row["message"] or ""))
        parts.append(str(row["from_ticket"] or ""))
        parts.append(_flatten_json_list(row["with_items_json"]))
    return "\n".join(part for part in parts if part)


def _ticket_record(ticket_dir: Path) -> tuple[str, str, str, str, str, str, str]:
    from .db import open_db

    conn = open_db(ticket_dir)
    try:
        meta = conn.execute("SELECT * FROM ticket_meta LIMIT 1").fetchone()
        cv = conn.execute("SELECT * FROM current_view WHERE singleton = 1").fetchone()
        notices_text = _notice_search_text(conn)
    finally:
        conn.close()
    if meta is None or cv is None:
        raise SystemExit(f"database is missing ticket rows: {ticket_dir}")

    title = str(meta["title"] or ticket_dir.name).strip()
    lifecycle_state = str(meta["lifecycle_state"] or "ACTIVE").strip().upper() or "ACTIVE"
    updated_at = str(meta["updated_at"] or "").strip()
    body_parts = [
        title,
        ticket_dir.name,
        str(ticket_dir),
        lifecycle_state,
        str(meta["source_ticket_dir"] or ""),
        str(meta["canonical_source"] or ""),
        str(meta["owner_label"] or ""),
        str(meta["owner_last_action"] or ""),
        str(meta["owner_last_actor_label"] or ""),
        str(meta["owner_last_action_at"] or ""),
        str(meta["squashed_into_ticket_dir"] or ""),
        str(meta["squashed_into_ticket_uri"] or ""),
        str(meta["squashed_into_at"] or ""),
        str(cv["goal"] or ""),
        str(cv["short_context"] or ""),
        _flatten_json_list(cv["must_remember"]),
        str(cv["scope_non_goals"] or ""),
        _flatten_json_list(cv["items"]),
        str(cv["links_extra_md"] or ""),
        str(cv["decisions_md"] or ""),
        str(cv["artifacts_md"] or ""),
        str(cv["work_log_md"] or ""),
        str(cv["env_md"] or ""),
        notices_text,
    ]
    body = "\n".join(part for part in body_parts if part)
    tokens = _search_text(*body_parts)
    return ("ticket", str(ticket_dir), title, lifecycle_state, updated_at, body, tokens)


def _delete_doc(conn: sqlite3.Connection, *, kind: str, path: str) -> None:
    conn.execute("DELETE FROM docs_fts WHERE kind = ? AND path = ?", (kind, path))


def _upsert_record(conn: sqlite3.Connection, record: tuple[str, str, str, str, str, str, str]) -> None:
    kind, path, title, lifecycle_state, updated_at, body, tokens = record
    _delete_doc(conn, kind=kind, path=path)
    conn.execute(
        """INSERT INTO docs_fts
           (kind, path, title, lifecycle_state, updated_at, body, tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (kind, path, title, lifecycle_state, updated_at, body, tokens),
    )


def upsert_ticket(ticket_dir: Path) -> None:
    root = _root_from_ticket_dir(ticket_dir)
    conn = _open_search_db(root)
    try:
        _upsert_record(conn, _ticket_record(ticket_dir))
        conn.commit()
    finally:
        conn.close()


def safe_upsert_ticket(ticket_dir: Path) -> None:
    try:
        upsert_ticket(ticket_dir)
    except Exception as exc:
        raise SystemExit(
            f"search index update failed for ticket {ticket_dir}: {exc}. "
            "The derived search index may be broken. Agent: notify the user that the search index is broken "
            "and ticket search results may be stale or unavailable; run `aticket-cli tickets search --reindex` "
            "after the underlying problem is fixed."
        ) from exc


def delete_ticket(ticket_dir: Path) -> None:
    root = _root_from_ticket_dir(ticket_dir)
    conn = _open_search_db(root)
    try:
        _delete_doc(conn, kind="ticket", path=str(ticket_dir))
        conn.commit()
    finally:
        conn.close()


def safe_delete_ticket(ticket_dir: Path) -> None:
    try:
        delete_ticket(ticket_dir)
    except Exception as exc:
        raise SystemExit(
            f"search index delete failed for ticket {ticket_dir}: {exc}. "
            "The derived search index may be broken. Agent: notify the user that the search index is broken "
            "and ticket search results may be stale or unavailable; run `aticket-cli tickets search --reindex` "
            "after the underlying problem is fixed."
        ) from exc


def reindex(root: Path) -> int:
    conn = _open_search_db(root)
    tickets_count = 0
    try:
        conn.execute("DELETE FROM docs_fts")
        tickets_dir = canonical_tickets_dir(root)
        if tickets_dir.exists():
            for ticket_dir in sorted(p for p in tickets_dir.iterdir() if p.is_dir()):
                db_path = ticket_dir / "state" / "ticket.sqlite3"
                if not db_path.exists():
                    continue
                _upsert_record(conn, _ticket_record(ticket_dir))
                tickets_count += 1
        conn.commit()
    finally:
        conn.close()
    return tickets_count


def cmd_search(args: argparse.Namespace) -> int:
    root = agent_tickets_root()
    if args.reindex:
        if getattr(args, "ticket", []):
            raise SystemExit("--ticket only applies to --query searches")
        tickets_count = reindex(root)
        print(f"reindexed\t{tickets_count}\t{search_db_path(root)}")
        return 0
    if not args.query.strip():
        raise SystemExit("search requires --query unless --reindex is set")

    scoped_paths = _scoped_ticket_paths(list(getattr(args, "ticket", []) or []), root=root)
    conn = _open_search_db(root)
    try:
        if scoped_paths is not None:
            conn.execute("CREATE TEMP TABLE search_scope(path TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO search_scope(path) VALUES (?)", [(path,) for path in scoped_paths])
        sql = (
            "SELECT kind, path, title, lifecycle_state, updated_at, body, "
            "bm25(docs_fts) AS score FROM docs_fts"
        )
        if scoped_paths is not None:
            sql += " JOIN search_scope USING (path)"
        sql += " WHERE docs_fts MATCH ? AND kind = 'ticket'"
        params: list[object] = [_fts_query(args.query)]
        wanted_lifecycle = args.lifecycle_state.strip().upper()
        if wanted_lifecycle != "ALL":
            sql += " AND lifecycle_state = ?"
            params.append(wanted_lifecycle)
        sql += " ORDER BY score, updated_at DESC LIMIT ?"
        params.append(int(args.limit))
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    if args.format == "json":
        payload = [
            {
                "kind": row["kind"],
                "path": row["path"],
                "title": row["title"],
                "lifecycle_state": row["lifecycle_state"],
                "updated_at": row["updated_at"],
                "score": row["score"],
                "snippet": _snippet(str(row["body"] or ""), args.query),
            }
            for row in rows
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    for row in rows:
        snippet = _snippet(str(row["body"] or ""), args.query)
        print(
            f"{row['kind']}\t{row['lifecycle_state']}\t"
            f"{row['updated_at']}\t{row['path']}\t{row['title']}\t{snippet}"
        )
    return 0
