---
name: ticket-workdir
description: "用 `aticket-cli` 管理 agent work ticket：短期作为当前工作容器，中期作为 compaction 后恢复上下文的备查记忆，长期作为可检索的知识/审计资产。Use when the work needs ticket, TICKET.md, agent-tickets, claim/release lease, session handoff, 交接, archive, 归档, fork ticket, follow-up work, or recovery notes. Ticket 是证据链/交接容器，不替代 git worktree 隔离；agent 自己决定写入哪些 log/item，并维护简洁 short-context。"
---

# Ticket Workdir（aticket-cli）

## Ticket 是什么：工作容器 + 备查记忆 + 可检索资产

Ticket 同时承担三层价值：

1. **当前工作的容器（短期：小时-天）**
   把命令输出、决策、产物落到 ticket 目录里，让中断恢复、跨 agent 交接有可靠锚点。

2. **agent 自己的备查记忆（中期：同一工作内）**
   长 session 的 context 会被 compaction 压缩。ticket 是恢复入口：先读 `goal`、`short-context`、`Must remember`、item URIs、最近 `Work log`，不用从头读完整历史。

3. **过去工作的可检索资产（长期：跨工作）**
   归档后的 ticket 是知识/流程资产和审计资产。后来的 agent 能搜索到过去的 PR、决策、item、artifact 文件、踩坑和结论。

写 ticket 时默认读者包括当前 agent、compaction 后的自己、以及后来的其他 agent。关键字段要 standalone，不能只写只有当前上下文才能懂的 anchor。

## Ticket vs worktree

Ticket 解决的是证据链 / 交接 / 资产沉淀；git worktree 解决的是仓库写路径隔离。创建 ticket 不等于已经满足 repo 写路径约束。任何会改仓库内文件的下一步，仍必须在第一次编辑前验证 cwd 是 linked worktree：

```bash
git rev-parse --git-dir
git rev-parse --git-common-dir   # 应该 != git-dir
pwd
git branch --show-current
```

## CLI capability preflight

使用本 skill 前先确认 PATH 上的 `aticket-cli` 已经是 brief/must-remember-capable resource-style 版本：

```bash
which aticket-cli
aticket-cli ticket --help >/dev/null
aticket-cli message --help >/dev/null
aticket-cli ticket /tmp/example-ticket brief --help >/dev/null
aticket-cli ticket /tmp/example-ticket remember --help >/dev/null
aticket-cli ticket /tmp/example-ticket forget --help >/dev/null
! aticket-cli notice --help >/dev/null 2>&1
```

如果 `aticket-cli ticket --help`、`aticket-cli message --help` 或 `brief` / `remember` / `forget` help 失败，说明当前环境仍是旧 CLI 或 PATH 指向错误。如果 `aticket-cli notice --help` 成功，说明环境仍是 notice-era resource CLI。不要继续执行 ticket 命令；先停止并让 human 更新 aticket-cli / ticket-workdir skill。旧 flat command 和旧 notice resource 本版本故意不兼容，也不会提供 runtime migration hint。

除 help 以外的有效命令都要求存在 config。默认路径是 `~/.config/aticket-cli/config.toml`；第一次有效非 help 命令会从包内模板创建默认文件，并在 stderr 提示已创建的 config 目录/文件，以及需要检查并修改 `[tickets].root`。`[identity].codex_jsonl_root` / `[identity].claude_jsonl_root` 是可选项，只在身份校验实际用到对应 provider session root 时才相关。显式设置 `ATICKET_CONFIG` 时不会隐式创建，缺失或不可读会直接失败，避免 agent 指向错误配置后继续写 ticket。

更新 installed ticket-workdir skill 时必须同步 `SKILL.md` 和整个 `references/` 目录，并删除旧的 installed `references/notice.md`；否则后续 agent 可能继续读到已经无效的 notice 命令。

## Ticket 数据目录

```
$AGENT_TICKETS_ROOT/             # or [tickets].root in ~/.config/aticket-cli/config.toml; default: /code/tsshi/agent-tickets
├── tickets/
│   └── <YYYY-MM-DD>-<topic>-<HHMMSS>/
│       ├── TICKET.md            # rendered view, do not hand-edit
│       ├── notes/               # 分析笔记、handoff notes
│       ├── artifacts/           # 不可变文件证据、快照、实验输出、较长附件
│       ├── workspace/           # scratch / cross-repo worktrees
│       └── state/               # machine state; CLI owns it
│           ├── ticket.sqlite3   # truth source, including sqlite-backed lease
│           └── fork.json        # fork metadata if this is a forked ticket
```

- `TICKET.md` 由 `aticket-cli` 维护；不要手编。
- Lifecycle 有三态：`BACKLOG` 表示已创建但未认领，`ACTIVE` 表示已 claim / 正在推进，`ARCHIVED` 表示已收口不可再改。
- claim/release lease 状态在 sqlite；claim 会把 `BACKLOG` ticket 切到 `ACTIVE`，release 会清空 active holder 并切回 `BACKLOG`；`TICKET.md` 的 `Owner:` 行只显示最近一次 `claim/release by ... at ...`。
- claim/new/fork/release 默认从当前 Codex/Claude 工具环境推断 owner，CLI 内部存成可校验的 `agents://<agent-type>/<session-id>`。只有不在 provider 工具子进程里运行时，才显式传 `--agent-type <codex|claude> --session-id <uuid>`。
- `aticket-cli ticket "$TICKET_DIR" claim --force` 覆盖不同 active holder 前必须先问 human 并获得接管同意；命令必须同时带 `--confirm-human-approved-takeover`。没有 human 同意时，用 fork/new ticket 做并行工作。
- `message` 用于在不接管 ticket 的情况下补充信息；见下方 Message 外部补充信息。
- `Lifecycle=ARCHIVED` 表示 ticket 已收口，后续不能再修改；如果还要继续，只能 fork 或开新 ticket。
- 大 ticket archive 的确认 flag 是轻量 closeout pause：归档前检查 `artifacts/` / `workspace/`，能压缩的 raw/detail 数据先压缩，并确保 ticket 已写清 final result 或 handoff state；超过 human 阈值时先问 human。

## 命令形态

本版本故意不兼容旧 flat command；旧命令格式就是错误格式。公开接口是本文档中的 resource-style 命令。

`aticket-cli message send --ticket <dir> ...` 是保留的外部 message 目标参数，因为这是外部 sender 给另一个 ticket 补充信息，不是在 holder 身份下操作该 ticket。`aticket-cli tickets search --ticket <dir>` 是只读搜索范围过滤。看完并标记 message 是 holder-side 操作，用 `aticket-cli ticket "$TICKET_DIR" message checked --until-id N`。

## Message 外部补充信息

`message` 是在不接管 ticket 的情况下，给该 ticket 补充信息的机制。当前 holder 或后续接管 ticket 的 agent 通过读取 `TICKET.md` 能看到 active message，从而避免这些补充信息丢失。

发送 message：

```bash
aticket-cli message send \
  --ticket "$TARGET_TICKET_DIR" \
  "One-line summary; details in linked note." \
  --with file:///path/to/details.md
```

规则：

- message 主内容是 positional message；真实换行会写成字面量 `\n`，保证 rendered ticket 中是一行。
- 长内容先写 markdown 文件（通常放在 `notes/` 或 `artifacts/`），再用 `--with file://...` 关联。
- holder 通过读 `TICKET.md` 接收 active message；任何成功的目标 ticket 命令如果发现该 ticket 有未读 active message，都会在 stderr 提醒 agent 去读 `## Messages`。
- holder 看完后用 `aticket-cli ticket "$TICKET_DIR" message checked --until-id N` 标记水位线。
- archived ticket 拒绝普通 active message；确实只想追加历史上下文时，用 `aticket-cli message send --ticket "$TICKET_DIR" --allow-archived "..."`，但这没有 active holder/session 投递保证。

## 4 个工作阶段（渐进式披露）

`SKILL.md` 只保留核心判断。需要命令细节、反例或阶段内流程时，只读取对应 reference：

| 阶段 | 你在做什么 | 命令清单 | 深度文档 |
|---|---|---|---|
| **Discover** | 找已有 ticket / 搜过往参考 | `ls` / `rg` / `aticket-cli tickets search` | [`references/discover.md`](references/discover.md) |
| **Claim** | 新建 / claim/release / fork 派生 ticket | `aticket-cli ticket new` / `aticket-cli ticket <dir> claim` / `aticket-cli ticket <dir> release` / `aticket-cli ticket <dir> fork` | [`references/claim.md`](references/claim.md) |
| **Work** | 在持有的 ticket 上记录进度、决策、产物，接收外部 message | `aticket-cli ticket <dir> brief` / `aticket-cli ticket <dir> goal` / `aticket-cli ticket <dir> context` / `aticket-cli ticket <dir> remember` / `aticket-cli ticket <dir> forget` / `aticket-cli ticket <dir> log` / `aticket-cli ticket <dir> add-item` / `aticket-cli message send` | [`references/work-on-held-ticket.md`](references/work-on-held-ticket.md), [`references/message.md`](references/message.md) |
| **Handoff & Archive** | 让 ticket 在 PR/doc/下一 agent 可被发现，然后收尾，或把已完成小票事后归并 | `aticket-cli ticket <dir> add-item` / `aticket-cli ticket <dir> archive` / `aticket-cli tickets squash` | [`references/handoff-and-archive.md`](references/handoff-and-archive.md) |

Discover 跨阶段共用：即使你已经在做某个 ticket，也常常需要 `aticket-cli tickets search` 找过往参考，或直接扫 ticket 文件：

```bash
ROOT="${AGENT_TICKETS_ROOT:-/code/tsshi/agent-tickets}"  # or [tickets].root in aticket config
ls -td "$ROOT"/tickets/* 2>/dev/null | head
rg -n '^Lifecycle:|^Owner:|^## Goal|^## Short context|^## Must remember' "$ROOT"/tickets/*/TICKET.md 2>/dev/null
aticket-cli tickets search --query "<keyword>" --ticket "$TICKET_A" --ticket "$TICKET_B"
```

## 最小写实合同

`aticket-cli ticket new` 和 `aticket-cli ticket <dir> fork` 必须带非空 `--goal`。如果会执行命令、产生产物或改文件，ticket 还至少需要：

1. 一条真实的 `aticket-cli ticket <dir> log`
2. 必要的 `aticket-cli ticket <dir> add-item`，把 PR、文档、关键路径、相关 ticket 作为 URI 记录下来
3. 一个简洁 `aticket-cli ticket <dir> context`，说明当前状态、下一步、重要 note 或暂缓事项
4. 必要时用 `aticket-cli ticket <dir> remember` 记录不能忘的 principle、preflight、invariant 或 human instruction；每个 ticket 最多 16 条，满了先用 `forget <index>` 删除一条；fork ticket 会继承这些条目

进入阶段边界（开始工作、claim/fork 后、context 压缩恢复后、repo 写操作前、release/archive 前）先运行：

```bash
aticket-cli ticket "$TICKET_DIR" brief
```

`brief` 会把 goal、short-context、Must remember、unread messages 和关键计数放到同一个输出里。普通 ticket 命令成功后，如果 ticket 有 Must remember，也会在 stderr 主动提醒这些条目。

`short-context` 是可覆盖的恢复摘要，不是流水账。通常几百字以内；关键状态变化、等待外部反馈、准备交接、收尾前更新一次即可，不需要每条 log 后都改。

## 发现新事情但现在不做

agent 在做 ticket A 时，可能发现一个新的可做事项，但当前不准备切过去做。aticket 不再有单独 task 池；基本单位就是 ticket。

- **只是 ticket A 的尾巴**：写进 A 的 `short-context`、`items` 或 `log`。适合“等 reviewer 回来继续修”“本 PR 合入后跑一次 smoke”这类仍属于 A 的后续动作。
- **是独立工作单元**：创建 ticket B，让之后的 agent 可以直接接手。适合“顺手发现另一个 bug”“另一个文档也该改”“后续可以单独做一次清理”。

如果 B 是 A 的派生线，并且 A 已有可恢复状态，用 `aticket-cli ticket "$A" fork --topic "<topic>" --goal "<goal>"`。fork 会先内部刷新 A 的 rendered view，再把 A 当时的 `TICKET.md` 快照放进 B 的 `artifacts/source-ticket-snapshot.md`，并把 A 的 `Must remember` 条目复制进 B；B 可以读 A 的 ticket/artifacts/state 来补上下文，但要清楚自己拿到的是 point-in-time snapshot，不能直接操作 A。

如果 B 只是另一个独立工作且以后再做，用 `aticket-cli ticket new --topic "<topic>" --goal "<goal>" --backlog"` 创建未认领 ticket；如果当前 agent 要立即推进，用不带 `--backlog` 的 `ticket new`。创建后在 B 里写清来源，必要时在 A 里用 `add-item file://...` 反链 B。

## 事后归并小票：squash

如果几个已经收口的短 ticket 后来被确认属于同一个 human-level intent，用 `tickets squash` 创建一个更大的普通 ticket：

```bash
BIG=$(aticket-cli tickets squash "$SMALL_A" "$SMALL_B" \
  --topic "<larger-topic>" \
  --goal "<大 ticket 后续要完成什么>" \
  --summary "<这些小票合起来已经做了什么>" \
  --next "<下一步>")
```

规则：

- 只 squash `ARCHIVED` source tickets；不要抢 active/backlog 工作。
- source tickets 不物理合并、不删除，仍然是证据链。
- squash target 是普通 ticket：默认 `ACTIVE` 方便当前 agent 继续推进；也可以 `--backlog` 或 `--archive`。
- target 写入 `state/squash.json`、source snapshots、source ticket links，并逐条吸收 source items / work log。
- source artifact 文件留在原 ticket 目录；target 只记录指向原路径的 artifact references。
- source tickets 会收到一条受控的 archived annotation，指向 squash target；successor pointer 只存在 sqlite `ticket_meta`，之后相关后续工作写入 target，不回源票继续写。
- 已归档的 squash target 可以作为后续 squash 的 source；它被处理为普通 archived source。新的 target 是继续入口，旧 target 保留为证据链节点并反链新 target。

## Discover 不是接管

干新工作之前先 `aticket-cli tickets search` 或文件系统扫描找已有 ticket：

- **BACKLOG ticket**：未认领的待接手工作；确认 goal 匹配后可以 claim。
- **自己持有 lease 的 ACTIVE ticket**：算 self-resume，可以直接复用。
- **别人 own 的 ACTIVE ticket**：默认不 claim。先和 human 确认是合并进去、接管、还是开新 ticket 只把那个当参考；只有 human 明确同意接管后才可 `aticket-cli ticket "$TICKET_DIR" claim --force --confirm-human-approved-takeover`。
- **ARCHIVED ticket**：历史参考，不 claim。直接读 `TICKET.md` / artifacts / notes，新工作开新 ticket 或 fork。

判断口径：`same goal, same repo, same handoff unit, your own session => reuse`。其他情况要么开新 ticket，要么先问 human。

## 同时持有多个 ticket

实现层不约束同一 agent 同时 own 多少 ticket。lease 是占位锁：保护 ticket 的 `workspace/` 和当前推进权，避免另一个 agent 默认写入同一工作面。

如果一个 ticket 暂时不推进但还需要保持原样，继续持有 lease 是合理的。切走前更新 `short-context` 和关键 item，让自己或接手者能恢复。如果这个 ticket 可以让别人接手，先 `aticket-cli ticket "$TICKET_DIR" release`；如果已经收口，执行 `aticket-cli ticket "$TICKET_DIR" archive`。

## Compaction 后从 ticket 恢复 context

恢复时优先看：

| 字段 | 用途 |
|---|---|
| `goal` | 一句话重建主线 |
| `short-context` | 当前状态、下一步、重要 note、暂缓事项 |
| `Must remember` | 最多 16 条 principle、preflight、invariant、human instruction；`TICKET.md` 用 Markdown 有序列表渲染，可用 `forget <index>` 删除对应编号；fork 会继承 |
| `Items` / item URIs | PR、文档、路径、agent thread 等原始 URI，便于筛选路径和链接 |
| `artifacts/` files | 不可变文件证据、快照、实验输出；入口应通过 item URI 指向 |
| `Work log` 最后几条 | 上次做到哪一步 |

实际操作：

```bash
sed -n '1,180p' "$TICKET_DIR/TICKET.md"
rg -n 'file://|https://|agents://' "$TICKET_DIR/TICKET.md"
```

如果连 `TICKET_DIR` 都丢了，用文件系统或 search 重新定位：

```bash
ROOT="${AGENT_TICKETS_ROOT:-/code/tsshi/agent-tickets}"  # or [tickets].root in aticket config
ls -td "$ROOT"/tickets/* 2>/dev/null | head
rg -n "<keyword>" "$ROOT"/tickets/*/TICKET.md 2>/dev/null
aticket-cli tickets search --query "<keyword>"
aticket-cli tickets search --query "<keyword>" --ticket "$TICKET_DIR"
```

## 写 ticket 的标准

- **goal**：一句话说明这个 ticket 要完成什么；创建时必须给，后续确实变更目标才用 `aticket-cli ticket <dir> goal`。
- **short-context**：恢复摘要；写当前状态、下一步、重要 note、暂缓事项，保持精简。
- **remember / forget**：不能忘的条目；写 principle、preflight、invariant、human instruction，list 存储，`TICKET.md` 用 Markdown 有序列表渲染，最多 16 条，`forget <index>` 删除对应编号，fork 继承。
- **item**：用 URI 记录可机器筛选的东西：`file://...`、`https://...`、`agents://...`。不要只写裸 URL；必要时在 log/context 里说明这是最终 PR、review dump、实验产物还是已废弃证据。
- **log**：关键决策要附依据； routine 进度可以简短。

## Reviewer wrapper 例外

由 review-agent-cli wrapper 托管持久化的 automated reviewer 可以声明：

```
Session: inherited (reviewer-wrapper-managed)
```

reviewer 本体不自己 `aticket-cli ticket new` / `aticket-cli ticket <dir> log`；wrapper 或父 ticket 负责持久化 reviewer notes、artifact files / PR comment。

## 安全底线

- 不要把 token / 密码 / 私钥落盘或提交进 git。
- 只记录环境变量名或引用方式（`$GITHUB_TOKEN`、`~/.config/example-tool/config.toml`）。
- agent 自己决定写入哪些 `log` / `item` / `short-context`；敏感信息不要写进 ticket。
