# PaperCode 复用清单

本仓库是比赛适配层，不直接运行 PaperCode。以下是把 PaperCode 中可复用的部分接进来的方式；完整 PaperCode 运行时（TUI / PaperRun 14 阶段 / provider 工作台）没有进入比赛镜像。

| PaperCode 资产 | 在本仓库的落点 | 用途 |
|---|---|---|
| `material-index.ts` 的 MaterialIndex 纪律 | `core/paperlib.py` 的 `MaterialRecord` / `index_materials` | 每篇指定 PDF 记录文件名、SHA-256、页数、标题、摘要与正文；B/C/E 共用同一份证据索引 |
| `research-tools` 的 PDF 思路（pypdf、PDF probe） | `core/paperlib.py` 的 `read_pdf` / `probe_pdf`、`tools.py` 的 `pdf_probe` / `read_pdf_text` / `read_materials` | B/C/E 读取本地论文正文；B 先剔除伪装成 .pdf 的非 PDF 文件 |
| `Gate-Honest` / `ClaimLedger` / `verification/checks.ts` | `handlers/evidence.py` 的 `Claim` / `build_ledger` / `audit_report` | 逐句检查事实性语句是否绑定 `[编号]`；未绑定、引用越界、缺参考文献都作为 blocker |
| `verification/manuscript.ts` 的章节与占位检查 | `handlers/evidence.py` 的 `_section_coverage` / `PLACEHOLDER_RE` | C/E 章节覆盖、800 字下限、TODO/待补充等占位标记 |
| `verification/gate.ts` 的 blocker / warning 分层 | `AuditReport.blockers` / `warnings` / `repair_text` | 只有 blocker 触发一次完整修复；warning 不阻断 |
| `NoveltyCheckpoint` / `PriorWorkMap` | `handlers/evidence.py` 的 `novelty_overlap`、`handlers/e_idea.py` 的 top-N 重合度提示 | E 先写已有工作覆盖与重合度，再论证差异与互补；重合度只是本地词项重叠，不冒充查新结论 |
| `PaperSpine` 的问题→动机→方法→证据→边界结构 | `handlers/e_idea.py` 的 `CHECKLIST` / system prompt | E 报告固定覆盖研究问题、动机、技术路线、基线、指标、互补、瓶颈、可行性、可证伪 |
| `writing/draft.ts` 的 forced draft / 证据摘录策略 | `core/solve.py` 的 `_fallback_report` | 模型调用失败时仍输出逐篇带 `[编号]` 的摘录式报告，而不是内容为空的「受限运行」占位 |
| `research/experiments` 的 reproduction gate / evidence 思路 | `d_adapter/selftest.py` | D 自测：真实子进程、真实 stdout/stderr、协议校验、失败回修，不伪造指标 |
| `model_source` 平台公开工具 | `d_adapter/source.py` | D 源码逐字提取（平台提供，非 PaperCode） |

## 新增的检索与预算改动

- `core/paperlib.py::bm25_rank`：本地语料 BM25（标题加权、IDF、长度归一），替代原来的裸词频计数；B 的候选池保持 15，并要求模型最多返回 5 篇、每篇覆盖至少两个检索要素。
- `core/paperlib.py::evidence_excerpt`：标题 + 摘要 + 与题目相关的关键句，替代直接截前 6000 字；C/E 的上下文更短、更相关。
- `handlers/reporting.py::finalize`：先 `ensure_coverage` 补齐缺失的正文引用和参考文献文件名（引用的是论文摘录，不是编造内容），再跑 `audit_report`。
- D 时间预算：案例 170 秒、正式实验预留 70 秒、自测默认最多 2 个样例 × 8 秒；自测失败只在剩余时间充足时修复，否则直接进正式实验入口。

## 没有复用的部分

- **PaperRun 14 阶段 / TUI / 桌面端**：适合写整篇论文，不适合 180 秒一道题；C/E 用一次生成 + 一次证据闸门修复即可。
- **provider 工作台 / 多模型路由**：比赛只允许赛事 Gateway，必须锁死单一出口。
- **网页采集 / MCP / Hugging Face loop**：正式评测禁止自定义 Tool 联网，不能使用。
- **PaperCode 的完整实验执行器**：协议是 `ExperimentProtocol.v1`，与赛题 `layered-d-v3` 不兼容；只借用「真实执行 + 证据绑定」的设计。
- **投稿级 manuscript gate 的全部规则**（20 条参考文献、统计显著性、图表数量等）：赛题 C/E 的目标不是投稿，只保留与评分相关的章节、引用、占位和证据检查。

## 许可

PaperCode 原创代码为 MPL-2.0；本仓库只按设计思路重写 Python 实现，不复制 PaperCode 运行时代码。若后续 vendor 具体文件，会在 `NOTICE.md` 记录来源 commit 与许可证。
