---
description: "Discover stage — 找已有 ticket、搜过往 ticket 作参考。即使已经在某个 ticket 内也常用：ticket 本身是信息来源。"
triggers:
  - "aticket-cli tickets search"
  - "找 ticket"
  - "搜 ticket"
  - "可复用 ticket"
  - "AGENT_TICKETS_ROOT"
---

# Discover — 找已有 ticket / 搜过往参考

Discover 是 ticket 工作流的入口阶段，目的有两个：

1. **避免重复劳动**：一个相似目标可能已经被别人或自己之前做过。
2. **拿历史做参考**：过去类似 ticket 的决策、踩坑、PR 是这次工作最好的起点。

找到相似 ticket 不等于应该 claim 它。discover 的默认动作是阅读和判断。

## 文件系统扫描

ticket 是普通文件空间。列目录和读 rendered `TICKET.md` 不需要 CLI：

```bash
ROOT="${AGENT_TICKETS_ROOT:-/code/tsshi/agent-tickets}"
ls -td "$ROOT"/tickets/* 2>/dev/null | head
rg -n '^Lifecycle:|^Owner:|^## Goal|^## Short context|^## Must remember' "$ROOT"/tickets/*/TICKET.md 2>/dev/null
rg -n "<keyword>" "$ROOT"/tickets/*/TICKET.md 2>/dev/null
```

只扫顶层 `tickets/*/TICKET.md`。不要递归扫整个 `tickets/` 树，否则会命中 ticket `workspace/` 里的模板、fixture 或 cloned repo 文件。

单个 ticket 内筛链接 / URI：

```bash
sed -n '1,180p' "$TICKET_DIR/TICKET.md"
rg -n 'file://|https://|agents://' "$TICKET_DIR/TICKET.md"
```

## aticket-cli tickets search

`search` 是保留的读接口，因为它是跨 ticket 的派生 FTS 索引，不等同于 `ls` / `rg` 单次读文件。

```bash
aticket-cli tickets search --query "<keyword>"                         # 默认 ALL lifecycle
aticket-cli tickets search --query "<keyword>" --kind ticket            # 仅搜 ticket
aticket-cli tickets search --query "<keyword>" --lifecycle-state BACKLOG # 未认领
aticket-cli tickets search --query "<keyword>" --lifecycle-state ACTIVE  # 已认领 / 推进中
aticket-cli tickets search --query "<keyword>" --ticket "$TICKET_A" --ticket "$TICKET_B" # 直接限定 ticket
aticket-cli tickets search --query "<keyword>" --limit 20               # 限制结果数
aticket-cli tickets search --query "<keyword>" --format json            # JSON 输出
```

索引重建：

```bash
aticket-cli tickets search --reindex
```

## 找到 same ticket 后如何处理

| 当前发现 | 默认行动 |
|---|---|
| **BACKLOG ticket** | 可认领的未开始/待接手工作；确认目标匹配后 `claim` |
| **自己持有 lease 的 ACTIVE ticket** | self-resume，直接 `export TICKET_DIR=<found-dir>` 复用 |
| **别人 own 的 ACTIVE ticket** | 不默认 claim；先和 human 确认：合并进那个 ticket / 接管它 / 开新 ticket 只把那个当参考 |
| **ARCHIVED ticket** | 历史参考，不 claim。直接读 `TICKET.md` / `artifacts/` / `notes/`，新工作开新 ticket 或 fork |
| **没找到可复用 ticket** | 开新：`aticket-cli ticket new --topic "<topic>" --goal "<goal>"` |
| **source ticket 已形成可恢复状态，要做独立派生工作** | `aticket-cli ticket "$SOURCE" fork --topic "<topic>" --goal "<goal>"` |

判断口径：`same goal, same repo, same handoff unit, your own session => reuse`。其他情况要么开新 ticket，要么问 human。

把别人的 active ticket claim 走有副作用：你会接管它的 workspace/current-progress lease，并可能改变对方回来时看到的 `short-context` 和工作状态。除非已经和 human 确认要接管，不要默认这么做。

## Discover 是否要落 ticket 本身？

文件系统扫描 / `aticket-cli tickets search` 这类只读查询本身不需要新建 ticket。只有当 discover 结果触发了实质工作（开 PR / 改文件 / 跑命令）时，才进入 Claim 阶段建立或接管 ticket。

## 反例

❌ 跳过 discover 直接 `aticket-cli ticket new`：每个对话开头都新建一个 ticket，导致语义重叠。

❌ discover 后忘了 export `TICKET_DIR`：找到了自己的 active ticket 决定 self-resume，但后续命令仍写到过期 ticket。

❌ 中途切到完全不同目标但不 re-discover：应该立刻搜有没有更合适的 ticket，而不是把异类工作塞进当前 ticket。

❌ 找到别人 active 的 same ticket 就直接 claim：先确认接管语义，或开新 ticket 把对方 ticket 当参考。
