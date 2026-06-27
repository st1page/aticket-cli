---
description: "Handoff & Archive stage — 让 ticket 在 PR / doc / 下一 agent 可被发现，并归档已收口 ticket。"
triggers:
  - "aticket-cli ticket <dir> archive"
  - "aticket-cli ticket <dir> add-item"
  - "PR description ticket 入口"
  - "documentation page ticket 入口"
  - "fork handoff"
  - "归档"
  - "lifecycle ARCHIVED"
---

# Handoff & Archive — 让 ticket 被找到 + 收口

ticket 工作的最后阶段：让 PR / 文档页面 / 下一个 agent 能反查到这个 ticket，并把已经收口的 ticket 归档。

## PR / 文档页面反查入口

任何会产出 PR 或文档页面的工作，都应该在外部产物里写清 ticket 入口，并在 ticket 里回链外部产物。

推荐写入外部产物的信息：

- Ticket directory: `$TICKET_DIR`
- TICKET.md: `$TICKET_DIR/TICKET.md`
- Artifact files directory when relevant: `$TICKET_DIR/artifacts/`
- 当前 PR / 文档 / branch / commit 的链接或路径

ticket 内部回链：

```bash
aticket-cli ticket "$TICKET_DIR" add-item "<url-or-path-uri>"
```

不要依赖固定 snippet。agent 根据 PR / doc 的格式自己组织文字，关键是路径和链接可被后续 agent 搜到。

## 交给后续 agent 的 follow-up ticket

当当前 ticket 里发现了独立 follow-up，但本轮不做，交接单位是新的 backlog ticket：

- 新 ticket 自己要可恢复：`goal` 和简洁 `short-context`。
- source ticket 要能反查：用 `add-item file://...` 回链新 ticket。
- `BACKLOG` 表示还没有 agent 认领推进；后续 agent `claim` 后进入 `ACTIVE`。

示例：

```bash
FOLLOWUP_DIR=$(aticket-cli ticket new \
  --topic "<followup-topic>" \
  --goal "<follow-up 要完成什么>" \
  --short-context "<由 $TICKET_DIR 发现；当前不做；下一 agent 第一动作为 ...>" \
  --backlog)
aticket-cli ticket "$TICKET_DIR" add-item "file://$FOLLOWUP_DIR"
```

如果 follow-up 是当前 ticket 的派生线，并且需要父 ticket 当时状态，用 `fork`：

```bash
FORK_DIR=$(aticket-cli ticket "$TICKET_DIR" fork \
  --topic "<followup-topic>" \
  --goal "<派生线要完成什么>")
aticket-cli ticket "$FORK_DIR" context "<下一 agent 接手后的第一步；必要背景>"
aticket-cli ticket "$TICKET_DIR" add-item "file://$FORK_DIR"
```

接手 forked ticket 的 agent 可以读 source ticket 和 source artifacts/state 来理解大局，但要记住 forked ticket 内的 `artifacts/source-ticket-snapshot.md` 才是 fork 创建时的父 ticket 快照。source ticket 后续可能已经变化。

fork 不会自动回写 source，也不会自动归档 source。forked ticket 完成后，按普通 ticket 方式记录产物、更新 `short-context`，然后 `archive` 或 `release`。如果 source ticket 也需要知道 fork 的结论，由持有 source lease 的 agent 显式 `add-item` / `log`。

## 把已完成小票归并成大 ticket

如果 agent 先前按局部 scope 创建并归档了几个短 ticket，后来确认它们其实属于一个更大的 human-level intent，用 squash 生成一个继续承载后续工作的普通 ticket：

```bash
BIG=$(aticket-cli tickets squash "$SMALL_A" "$SMALL_B" \
  --topic "<larger-topic>" \
  --goal "<大 ticket 后续要完成什么>" \
  --summary "<这些小票合起来已经做了什么>" \
  --next "<下一步>")
```

默认生成的 squash target 是 `ACTIVE`，当前 agent 可以继续推进。只想整理入口但不立即推进时加 `--backlog`；只是总结收口时加 `--archive`。

squash 不是新 lifecycle，也不是物理合并：

- source tickets 必须已经 `ARCHIVED`。
- source tickets 保持 archived evidence，不删除、不搬目录。
- target 写入 `state/squash.json`、source snapshots、source links 和继续工作的 `goal` / `short-context`。
- target 逐条吸收 source tickets 的 `items` 和 `work_log`，并标明来源。
- source artifact 文件留在原 ticket 目录；target 只记录指向原路径的 artifact references，避免相对路径搬家后失真。
- source tickets 会收到一条受控的 archived annotation，指向 target，满足从小票反查大票；source successor pointer 只存在 sqlite `ticket_meta`。
- squash 后相关后续工作写到 target，不回 source tickets 继续写。
- 已归档的 squash target 后续也可以作为 source 继续被 squash；这表示入口从旧 target 转移到新 target，而不是展开或删除旧证据链。

## 归档 ticket

完成所有动作后，归档 ticket：

```bash
aticket-cli ticket "$TICKET_DIR" archive
```

`Lifecycle=ARCHIVED` 是 sqlite 字段，不影响目录路径。归档后 ticket 目录仍在 `tickets/` 下，可以通过 `ls`、`rg` 或 `aticket-cli tickets search` 找到：

```bash
ROOT="${AGENT_TICKETS_ROOT:-/code/tsshi/agent-tickets}"
ls -td "$ROOT"/tickets/* 2>/dev/null | head
rg -n "<keyword>" "$ROOT"/tickets/*/TICKET.md 2>/dev/null
```

归档是不可修改边界：归档后的 ticket 不能再 claim/release/goal/context/log/add-item 写入。如果还要继续，只能 fork 或开新 ticket。

## Lifecycle 对照

| 任务类型 | 流程 |
|---|---|
| Automated reviewer（wrapper-managed） | `Session: inherited (reviewer-wrapper-managed)` → reviewer 只审不改 → wrapper / 父 ticket 持久化 reviewer notes、artifact files / PR comment |
| Backlog follow-up（只记录、不推进） | `aticket-cli ticket new --topic <t> --goal <goal> --backlog` → 后续 agent `claim` 后进入 `ACTIVE` |
| 短任务（修 typo / 回答 / 快速查询） | 创建 → 记录结果 → `aticket-cli ticket <dir> archive` |
| 长任务（功能 / 调研 / 数据迁移） | 创建 → 持续 `log` / `add-item` / `context` → 等反馈时补 handoff 合同 → 完成后 `aticket-cli ticket <dir> archive` |

## 反例

❌ PR description 不写 ticket 入口：reviewer / 接手 agent 无法反查到证据链。

❌ 只在外部 PR 写 ticket，不在 ticket 里回链 PR：后续搜索 ticket 时找不到最终产物。

❌ forked ticket 完成后假设 source 会自动更新：fork 是独立 ticket，source 是否更新由持有 source lease 的 agent 显式决定。

❌ 手动 mv ticket 目录改“归档”：lifecycle 是 sqlite 字段，移目录不是归档，还会破坏路径引用。
