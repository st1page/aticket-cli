---
description: "Claim stage — 新建 ticket、claim/release sqlite lease、fork 派生 ticket。"
triggers:
  - "aticket-cli ticket new"
  - "aticket-cli ticket <dir> claim"
  - "aticket-cli ticket <dir> release"
  - "aticket-cli ticket <dir> fork"
  - "新建 ticket"
  - "接管 ticket"
  - "释放 ticket"
  - "fork ticket"
  - "派生工作"
---

# Claim — 新建 / 接管 / 释放 / fork ticket

Claim 阶段：在 [Discover](discover.md) 之后，决定开一个新 ticket、接手别人释放或移交的 ticket，或从 source ticket fork 一个派生 ticket。

## Lease 语义

ticket 的 claim 是 **sqlite-backed lease**：保护当前 ticket 的 `workspace/` 和当前推进权。lease 状态保存在 `state/ticket.sqlite3` 的 `ticket_meta`，通过 sqlite `BEGIN IMMEDIATE` 原子更新；不要用 sidecar 文件判断或修改 owner。

`TICKET.md` 只渲染最近一次 owner 操作：

```text
Owner: claim by <holder> at <timestamp>
Owner: release by <holder> at <timestamp>
```

这行是人读视图；真正的互斥状态在 sqlite。

## 新建 ticket

```bash
TICKET_DIR=$(aticket-cli ticket new \
  --topic "<topic>" \
  --goal "<这个 ticket 要完成什么>")
```

- `--topic` 必须，出现在目录名里。
- `--goal` 必须非空，写清 ticket 的目标。
- `--short-context` 可选；如果已经知道当前状态/下一步，可以创建时直接写。
- 默认根目录是 `$AGENT_TICKETS_ROOT`（默认 `/code/tsshi/agent-tickets`）。
- lease holder 默认从当前 agent 工具环境推断：Codex 用 `CODEX_THREAD_ID`，Claude 用 `CLAUDE_CODE_SESSION_ID`。
- CLI 内部会拼成 `agents://<agent-type>/<session-id>` 存进 sqlite，并检查该 session 在本机 provider session store 中确实存在：Codex 查 `${CODEX_JSONL_ROOT:-~/.codex/sessions}`，Claude 查 `${CLAUDE_JSONL_ROOT:-~/.claude/projects}`。
- 如果不在 provider 工具子进程里运行，显式传 `--agent-type <codex|claude> --session-id <uuid>`；两个参数必须成对出现。
- 没有 user/host/pane fallback；不能靠 `TMUX_PANE`、用户名、hostname 伪造 owner。
- `--owner-label` 只影响 `TICKET.md` 里的可读显示，不参与 lease 判断。

默认新建 ticket 会立即 claim 它自己的 lease，初始化 `state/ticket.sqlite3`，并把 `TICKET.md` 渲染成 `managed-by: sqlite` 视图。

如果只是记录以后再做、当前 agent 不推进，用 backlog 新建：

```bash
aticket-cli ticket new --topic "<topic>" --goal "<goal>" --backlog
```

backlog ticket 初始化为 `Lifecycle: BACKLOG`，没有 active owner，也不会写 claim work-log。后续 agent 用 `aticket-cli ticket "$TICKET_DIR" claim` 接手时会切到 `ACTIVE`。

新建后立刻走 [work-on-held-ticket.md](work-on-held-ticket.md) 的最小写实合同：至少写真实 log，并在需要恢复或交接时维护 `short-context` 和关键 item。

## 接管已有 ticket

一个 agent 退出、释放、被中断，或 human 明确要求交接后，另一个 agent 可以 claim 同一个 ticket：

```bash
aticket-cli ticket "$EXISTING" claim
aticket-cli ticket "$EXISTING" claim --force --confirm-human-approved-takeover
```

接管行为细节：

- sqlite 事务里检查当前 holder；不同 holder 默认拒绝。
- 覆盖不同 active holder 前必须先问 human 并获得接管同意；`--force` 还必须带 `--confirm-human-approved-takeover`，表示 human 已明确批准接管。
- 如果没有 human 同意，或 human 要求并行推进，不要 force takeover；fork/new ticket，并使用自己的 workspace/branch 做并行工作。
- claim 时记录 `Claimed ticket by ...`，如果是 takeover 还会记录 previous holder，并把 lifecycle 切到 `ACTIVE`。
- `TICKET.md` 的 `Owner:` 行显示最近一次 claim 操作。
- archived ticket 不能 claim；要继续只能 fork 或开新 ticket。

## 释放 ticket

如果当前 agent 不再推进这个 ticket，但希望别人可以正常接手，release lease：

```bash
aticket-cli ticket "$TICKET_DIR" release
```

release 行为细节：

- 只有当前 holder 可以 release；如果需要清理异常残留，用 `--force`。
- sqlite 中清空 active holder，把 lifecycle 切到 `BACKLOG`，同时记录最近一次 `release by ... at ...`。
- work log 记录 `Released ticket by ...`。
- `TICKET.md` 的 `Owner:` 行显示最近一次 release 操作。
- release 不等于归档；完成收口后用 `aticket-cli ticket "$TICKET_DIR" archive`。

## 从当前 ticket 发现独立 follow-up

如果你正在做 ticket A，想到一个新的可做事项但现在不准备做，先判断它是不是能独立验收：

- 不能独立验收，只是 A 的尾巴：留在 A 的 `short-context` / `items` / `log`。
- 能独立验收，需要另一个 agent 以后接手：开 ticket B。

用 `aticket-cli ticket new` 开独立 ticket：

```bash
FOLLOWUP_DIR=$(aticket-cli ticket new \
  --topic "<followup-topic>" \
  --goal "<这个 follow-up 要完成什么>" \
  --short-context "<由 $TICKET_DIR 发现；当前不做。下一 agent 接手后的第一步；必要背景>" \
  --backlog)

aticket-cli ticket "$TICKET_DIR" add-item "file://$FOLLOWUP_DIR"
```

如果 follow-up 需要继承 A 的上下文、产物、决策记录或 `Must remember` 条目，用 `aticket-cli ticket "$TICKET_DIR" fork`：

```bash
FOLLOWUP_DIR=$(aticket-cli ticket "$TICKET_DIR" fork \
  --topic "<followup-topic>" \
  --goal "<派生线要完成什么>")
aticket-cli ticket "$FOLLOWUP_DIR" context "<下一 agent 接手后的第一步；必要背景>"
```

## Fork 派生 ticket

概念上 `fork` 是一种特殊的 `new`：它创建一个新的独立 ticket，并在初始化时多写入 source context。

```bash
FORK_DIR=$(aticket-cli ticket "$TICKET_DIR" fork \
  --topic "<new-topic>" \
  --goal "<fork ticket 要完成什么>" \
  --copy-path notes/handoff.md)
```

`fork` 会先内部刷新 source rendered view，再从 source 的 `TICKET.md` 取快照，并把完整快照存进 forked ticket 的 `artifacts/source-ticket-snapshot.md`。

最小语义：

- 创建新的 `TICKET_DIR`，初始化它自己的 sqlite + lease，`Lifecycle=ACTIVE`。
- 写入 `Forked from` / `Snapshot taken at` / `Canonical source after fork`，并落机器可读 `state/fork.json`。
- 写入 source snapshot，指向 forked ticket 空间里的 point-in-time 父 ticket 快照。
- 继承 source ticket 的 `Must remember` list 条目，让 principle、preflight、invariant、human instruction 不会只埋在父 ticket 日志里。
- `--copy-path` 只接受 source ticket 内的相对路径，用于把明确需要的父 ticket note/artifact 复制进 forked ticket。

fork 后各方行为：

- **source ticket**：保留原 sqlite / lifecycle / lease；fork 不是 claim/handoff，默认不会被回写。
- **forked ticket**：有自己的 goal、short-context、work log、artifacts 和 lease。
- **lease**：forked ticket 会像 `aticket-cli ticket new` 一样被当前 agent claim；它和 source ticket 的 lease 完全独立。
- **snapshot**：forked ticket 可以读 source ticket 当前内容，但必须知道自己空间里的 source snapshot 才是 fork 时刻的父状态。

fork 限制：

- 默认不回写 source ticket。
- 禁止复制运行态路径（`state/` / `workspace/`）。
- material path 之间不能重叠或互为祖先/子孙。
- fork 前预检所有 `--copy-path` 是否存在；预检失败不会创建 target。

forked ticket 完成后，用 `add-item` 记录产物和关联 ticket，再按普通 ticket 语义 `archive` 或 `release`。
