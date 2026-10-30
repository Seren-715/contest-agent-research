# 创新功能与可复核证据

面向 30% 专家复核部分。所有条目都对应仓库中的真实实现，不是概念描述。

## 1. Evidence-bound 写作：PaperCode Gate-Honest / ClaimLedger

- **实现**：`handlers/c_survey.py`、`handlers/e_idea.py` 的 system prompt 要求每条事实/数字/方法归因绑定 `[编号]`，区分论文报告值、作者观点与归纳；`handlers/reporting.py` 校验每篇指定论文在正文和 `## 参考文献` 中都出现。
- **可复核证据**：`uv run pytest tests/test_handlers_papers.py` 用 5 篇合成 PDF 跑通 B/C/E；C/E 的 JSON `markdown` 与另存文件逐字一致。
- **边界**：没有把 PaperCode 运行时搬进镜像；只复用其证据纪律和 PDF probe 思路，见 `docs/PAPERCODE_REUSE.md`。

## 2. D 类「来源准入 + 真复现」闭环

- **实现**：`d_adapter/gateway.py` 在当前 `/solve` 会话通过赛事 Gateway 生成完整 `run_experiment`，`d_adapter/source.py` 逐字提取（只允许换行归一）；`d_adapter/selftest.py` 用真实 `worker.py` 子进程跑公开样例，校验 D-01/D-02/D-05 的轨迹/排名/模型结构；失败时把完整错误发回同一模型，要求**完整修复版**，不本地改字；`d_adapter/pipeline.py` 受 180 秒预算约束。
- **可复核证据**：
  - `vendor/reference/D-01.py` 通过 3/3 公开样例；
  - `D-02.py` 通过 3/3；
  - `D-05.py` 通过 7/7；
  - 远程模型 `deepseek-v4-flash-vision-exp` 现场生成：D-01 4473 字节 / 3-3，D-02 8753 字节 / 3-3，D-05 11885 字节 / 7-7，均一次通过；
  - `tests/test_d_selftest.py` 覆盖「截断回复重新生成」和「自测失败后完整修复」两条路径。
- **边界**：参考实现只在 `vendor/reference/`（git-ignored，不进镜像），正式源码仍必须由 Gateway 现场生成。

## 3. 诚实降级，不伪造完成

- **实现**：`core/solve.py` 的 `failed_reproduction` 在 D 无法执行时保留三项指标对象、`observed=null`、`status=failed`；A 类模型失败时返回合法 JSON 兜底；C/E 模型失败时写「受限运行」报告，逐篇引用指定材料并明确说明未核验，不编造事实。
- **可复核证据**：`tests/test_fallbacks.py`、`tests/test_solve.py`；官方镜像 D 失败兜底实测输出三项 null 指标。
- **价值**：失败/超时不再直接变成非法输出；合法失败按平台规则保留状态。

## 4. SDK 原生采集

- **实现**：`tools.py` 的自定义工具全部带 `@tool`；`core/llm.py` 优先使用 `openai.OpenAI`，让 AgentHack SDK 采集 model/usage/tokens。
- **可复核证据**：官方镜像 + 本地 mock 网关五类联调中，`/solve` metadata 出现 `llm_calls`、`total_tokens`、`tools_used`（D 类还出现 `save_generated_source`）。
- **Token 效率**：C/E 单篇摘录 4500–6000 字符；B 候选 10 条、每条 900 字符；D 生成 `max_tokens=16000`；降级路径不再额外调用模型。

## 5. 与 PaperCode 的关系

- **复用**：`research-tools` 的 PDF probe 思路、Gate-Honest/ClaimLedger、NoveltyCheckpoint/PaperSpine、reproduction gate 的「真实执行 + 证据绑定」设计。
- **未复用**：PaperRun 14 阶段、TUI/桌面端、多 provider 工作台、联网采集；这些不适合 180 秒/题和离线评测。
- **许可**：PaperCode 原创代码 MPL-2.0；本仓库未复制其运行时代码；详见 `NOTICE.md`。

## 6. 复现命令

```bash
uv run pytest -q
uv run python scripts/d_selftest.py --case D-01 --implementation vendor/reference/D-01.py
uv run python scripts/d_selftest.py --case D-02 --implementation vendor/reference/D-02.py
uv run python scripts/d_selftest.py --case D-05 --implementation vendor/reference/D-05.py
```

官方镜像冒烟见 `docs/PLATFORM.md`；提交步骤见 `docs/SUBMIT.md`。
