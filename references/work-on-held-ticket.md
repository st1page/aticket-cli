---
description: "Work stage — 在持有的 ticket 上记录进度、决策、产物，并维护简洁 short-context。"
triggers:
  - "aticket-cli ticket <dir> log"
  - "aticket-cli ticket <dir> context"
  - "aticket-cli ticket <dir> goal"
  - "aticket-cli ticket <dir> brief"
  - "aticket-cli ticket <dir> remember"
  - "aticket-cli ticket <dir> forget"
  - "aticket-cli ticket <dir> add-item"
  - "最小写实合同"
  - "short-context"
  - "TICKET.md 空 scaffold"
---

# Work on Held Ticket — 在持有的 ticket 上记录进度

这是 ticket 工作流的核心阶段：你已经通过 [Discover](discover.md) + [Claim](claim.md) 拿到了 `$TICKET_DIR`，现在在这个 ticket 上做实质工作。

## 最小写实合同

`aticket-cli ticket new` / `aticket-cli ticket <dir> fork` 已经要求非空 `--goal`。如果会执行命令、产生产物或改文件，至少补齐：

1. 一条真实的 `aticket-cli ticket <dir> log`
2. 关键 URI 用 `aticket-cli ticket <dir> add-item`
3. 必要时写 `aticket-cli ticket <dir> context`

```bash
aticket-cli ticket "$TICKET_DIR" log "<时间或动作>: <做了什么 / 关键结论>"
aticket-cli ticket "$TICKET_DIR" remember "<不能忘的 principle / preflight / invariant / human instruction>"
aticket-cli ticket "$TICKET_DIR" add-item "file:///abs/path/or/https://..."
aticket-cli ticket "$TICKET_DIR" context "<当前状态；下一步；有用 note；暂缓事项>"
```

如果 substantive work 已经开始，但 `TICKET.md` 仍接近初始 scaffold（没有真实 log / item，`short-context` 无法恢复工作状态），视为 ticket hygiene 失败，先补齐再继续。

## 追加型字段

每次写入都追加一行，不覆盖历史：

```bash
aticket-cli ticket "$T" log "<时间或动作>: <做了什么>"
aticket-cli ticket "$T" brief
aticket-cli ticket "$T" remember "<不能忘的约束或检查>"
aticket-cli ticket "$T" forget 2
aticket-cli ticket "$T" add-item "<URI>"
```

支持的 URI scheme：

- `file:///abs/path` — 绝对路径
- `https://host/page` — Web URL
- `agents://codex/<thread-id>` — Codex agent thread
- `agents://claude/<session-id>` — Claude agent session

## 替换型字段

只有两个面向 agent 的覆盖字段：

```bash
aticket-cli ticket "$T" goal "<新的非空目标>"
aticket-cli ticket "$T" context "<简洁恢复摘要>"    # 或 --file path
```

`goal` 是 ticket 的目标，创建/fork 时必填。只有目标真的变化时才用 `aticket-cli ticket "$T" goal`。

`short-context` 是简洁恢复摘要，通常几百字以内。写当前状态、下一步、重要 note、暂缓事项；不要把它当 append log，也不要每次小动作都改。

`remember` 追加到 `## Must remember`，用于 principle、preflight、invariant、human instruction 这类不能埋进流水日志的内容。它按 list 存储，并在 `TICKET.md` 里用 Markdown 有序列表渲染；每个 ticket 最多 16 条；满了必须先用 `aticket-cli ticket "$T" forget <index>` 删除对应编号再添加。fork ticket 会继承这些条目。

`brief` 是阶段边界的主动读路径：开始工作、claim/fork 后、context 压缩恢复后、repo 写操作前、release/archive 前先运行，确认 goal、short-context、Must remember、unread messages 和关键计数。普通 ticket 命令成功后，如果 ticket 有 Must remember，也会在 stderr 主动提醒这些条目。

## 只读查询

```bash
sed -n '1,180p' "$T/TICKET.md"                    # 读 rendered ticket view
rg -n 'file://|https://|agents://' "$T/TICKET.md"  # 筛 URI / 外部链接
```

写命令会自动 re-render `TICKET.md`。`TICKET.md` 是 rendered view（带 `<!-- managed-by: sqlite -->` 标记），不要手编；手编会被下一次写命令覆盖。

## 等待反馈 / 交接的最小合同

任务还没完成但预计会等 reviewer、隔天继续、或让另一 agent 接手时，至少补齐：

- `Goal`：这次实际想推进什么
- `Short context`：当前状态、等待什么、下一步具体动作
- `Must remember`：仍然必须遵守的 principle、preflight、invariant、human instruction；最多 16 条，按编号删除
- `Items` / item URIs：PR、文档、关键路径、相关 ticket 的 URI；必要时在 log/context 里说明哪份是最终/当前有效

强烈推荐在 `notes/` 下写简短 handoff note（讨论较长时由 agent 主动摘要），再用 `aticket-cli ticket <dir> add-item file://...` 回链。

## Rescue 旧 ticket（最小补档）

接手一个 ticket，发现目录里有 artifact 但 `TICKET.md` 仍接近模板态：

1. 先看 `artifacts/` / `notes/` / 关联 worktree/PR，判断真实目标
2. 如果目标不准，用 `aticket-cli ticket "$T" goal` 回填
3. `aticket-cli ticket "$T" add-item "<URI>"` 补关键文件、PR 或文档的机器可筛选入口
4. 必要时用 `aticket-cli ticket "$T" log "<说明>"` 补哪份是最终产物
5. 必要时用 `aticket-cli ticket "$T" remember "<不能忘的约束>"` 补 principle / preflight / invariant
6. `aticket-cli ticket "$T" context` 写清当前状态、下一步动作或等待条件

## 反例

❌ `aticket-cli ticket new` 完直接开始写代码：没有任何 log / item，ticket 不能恢复工作状态。

❌ 只在对话里说“我跑了 X，结果是 Y”，没有 `aticket-cli ticket <dir> log`：下一个 agent 接手时看不到证据链。

❌ 手编 `TICKET.md`：会被下一次写命令自动 render 覆盖。

❌ 把 `short-context` 当流水账：它是恢复摘要，历史过程放 `log`，外部证据放 `add-item`。
