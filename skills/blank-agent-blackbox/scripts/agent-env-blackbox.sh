#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
out_dir="${ATICKET_BLACKBOX_OUT_DIR:-/tmp/aticket-agent-env-blackbox-${timestamp}}"
timeout_seconds="${ATICKET_BLACKBOX_TIMEOUT:-600}"
python_bin="${ATICKET_BLACKBOX_PYTHON:-python3.12}"
codex_model="${ATICKET_BLACKBOX_CODEX_MODEL:-gpt-5.4}"
claude_model="${ATICKET_BLACKBOX_CLAUDE_MODEL:-sonnet}"

mkdir -p "$out_dir"

if ! command -v aticket-cli >/dev/null 2>&1; then
  echo "aticket-cli entrypoint not found on PATH. Install with: pip install -e $repo_root" >&2
  exit 2
fi

run_prompt() {
  local topic="$1"
  local fork_topic="$2"
  cat <<EOF
Use the \`aticket-cli\` command to create a new ticket with \`aticket-cli ticket new\` using topic \`${topic}\` and a non-empty goal. Then append one work log line with \`aticket-cli ticket <dir> log\`, write a concise context with \`aticket-cli ticket <dir> context\`, send a short message to that same ticket with \`aticket-cli message send --ticket <dir>\`, mark that message checked with \`aticket-cli ticket <dir> message checked\`, release it with \`aticket-cli ticket <dir> release\`, claim it again with \`aticket-cli ticket <dir> claim\`, fork it with \`aticket-cli ticket <dir> fork\` to topic \`${fork_topic}\` with a non-empty goal, then release the fork.

The ticket root is already provided in AGENT_TICKETS_ROOT by the parent process.
Do not pass --agent-type or --session-id anywhere; let aticket-cli infer the current agent identity from the tool environment.
At the end, print the ticket paths and any owner/holder information shown by the CLI.
EOF
}

validate_sqlite_owner() {
  local provider="$1"
  local root="$2"
  "$python_bin" - "$provider" "$root" <<'PY'
import pathlib
import sqlite3
import sys

provider = sys.argv[1]
root = pathlib.Path(sys.argv[2])
tickets_dir = root / "tickets"
if not tickets_dir.is_dir():
    raise SystemExit(f"missing tickets directory: {tickets_dir}")

tickets = sorted(path for path in tickets_dir.iterdir() if path.is_dir())
if len(tickets) < 2:
    raise SystemExit(f"expected at least two tickets under {tickets_dir}, found {len(tickets)}")

prefix = f"agents://{provider}/"
seen_actor = False
for ticket in tickets:
    db = ticket / "state" / "ticket.sqlite3"
    if not db.is_file():
        raise SystemExit(f"missing sqlite state: {db}")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute(
        """
        select owner_id, owner_last_actor_id, owner_last_action
        from ticket_meta
        limit 1
        """
    ).fetchone()
    con.close()
    if row is None:
        raise SystemExit(f"missing ticket_meta row in {db}")
    for column in ("owner_id", "owner_last_actor_id"):
        value = row[column]
        if value is None:
            continue
        if not value.startswith(prefix):
            raise SystemExit(f"{ticket.name}: {column}={value!r}, expected prefix {prefix!r}")
        seen_actor = True

if not seen_actor:
    raise SystemExit(f"no {prefix} owner actor was recorded under {tickets_dir}")
print(f"{provider}: sqlite owner ids validated under {tickets_dir}")
PY
}

validate_no_explicit_owner_flags() {
  local provider="$1"
  local log_path="$2"
  "$python_bin" - "$provider" "$log_path" <<'PY'
import json
import pathlib
import re
import sys

provider = sys.argv[1]
log_path = pathlib.Path(sys.argv[2])
lifecycle = re.compile(r"\baticket-cli\s+ticket\b[^\n;&|]*\b(new|claim|release|fork)\b")
owner_flag = re.compile(r"--(agent-type|session-id)\b")

def fail_if_bad(command: str) -> None:
    if lifecycle.search(command) and owner_flag.search(command):
        raise SystemExit(f"{provider}: lifecycle command passed explicit owner flags: {command}")

if provider == "codex":
    for line in log_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        item = event.get("item") or {}
        if item.get("type") == "command_execution":
            fail_if_bad(item.get("command") or "")
elif provider == "claude":
    result = json.loads(log_path.read_text(encoding="utf-8"))
    session_id = result.get("session_id")
    if not session_id:
        raise SystemExit("claude: output json did not include session_id")
    candidates = sorted(pathlib.Path.home().glob(f".claude/projects/*/{session_id}.jsonl"))
    if not candidates:
        raise SystemExit(f"claude: could not find local transcript for session {session_id}")
    for line in candidates[-1].read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message") or {}
        content = message.get("content") or event.get("content") or []
        if isinstance(content, dict):
            content = [content]
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash":
                fail_if_bad((block.get("input") or {}).get("command") or "")
else:
    raise SystemExit(f"unsupported provider: {provider}")

print(f"{provider}: lifecycle commands did not pass --agent-type/--session-id")
PY
}

run_codex() {
  local root="$out_dir/codex-root"
  local topic="env-root-codex-${timestamp}"
  local fork_topic="${topic}-fork"
  mkdir -p "$root"
  echo "$root" > "$out_dir/codex-root.txt"

  env \
    AGENT_TICKETS_ROOT="$root" \
    PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}" \
    timeout "$timeout_seconds" \
    codex exec \
      --model "$codex_model" \
      --sandbox danger-full-access \
      --cd "$repo_root" \
      --json \
      --output-last-message "$out_dir/codex-last-message.txt" \
      "$(run_prompt "$topic" "$fork_topic")" \
      > "$out_dir/codex-events.jsonl" \
      2> "$out_dir/codex-stderr.log"

  validate_sqlite_owner codex "$root"
  validate_no_explicit_owner_flags codex "$out_dir/codex-events.jsonl"
}

run_claude() {
  local root="$out_dir/claude-root"
  local topic="env-root-claude-${timestamp}"
  local fork_topic="${topic}-fork"
  mkdir -p "$root"
  echo "$root" > "$out_dir/claude-root.txt"

  env \
    AGENT_TICKETS_ROOT="$root" \
    PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}" \
    timeout "$timeout_seconds" \
    claude -p \
      --permission-mode bypassPermissions \
      --model "$claude_model" \
      --output-format json \
      "$(run_prompt "$topic" "$fork_topic")" \
      > "$out_dir/claude-output.json" \
      2> "$out_dir/claude-stderr.log"

  validate_sqlite_owner claude "$root"
  validate_no_explicit_owner_flags claude "$out_dir/claude-output.json"
}

{
  echo "# aticket agent env blackbox"
  echo
  echo "- repo: $repo_root"
  echo "- out_dir: $out_dir"
  echo "- codex_model: $codex_model"
  echo "- claude_model: $claude_model"
  echo "- timeout_seconds: $timeout_seconds"
} > "$out_dir/SUMMARY.md"

if [[ "${ATICKET_BLACKBOX_SKIP_CODEX:-0}" != "1" ]]; then
  run_codex
  {
    echo
    echo "## Codex"
    echo
    echo "- root: $(cat "$out_dir/codex-root.txt")"
    echo "- final: $out_dir/codex-last-message.txt"
    echo "- events: $out_dir/codex-events.jsonl"
  } >> "$out_dir/SUMMARY.md"
fi

if [[ "${ATICKET_BLACKBOX_SKIP_CLAUDE:-0}" != "1" ]]; then
  run_claude
  {
    echo
    echo "## Claude"
    echo
    echo "- root: $(cat "$out_dir/claude-root.txt")"
    echo "- output: $out_dir/claude-output.json"
  } >> "$out_dir/SUMMARY.md"
fi

echo "agent env blackbox passed: $out_dir"
