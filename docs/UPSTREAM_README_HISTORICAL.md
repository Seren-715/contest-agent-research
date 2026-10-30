# contest-agent

![test](https://github.com/S0ra-ai/contest-agent/actions/workflows/test.yml/badge.svg)

2026 CCF 网络大会 · 第一届全国高校 Agent 网络创新挑战赛 · 赛题 1「科研助手」参赛 Agent。

这个仓库是**比赛适配层**：负责官方 AgentHack SDK 的 `solve()`、11 题分派、五类输出、D 类源码准入流程和结果校验。它不预置任何 D 类算法。

## 本分支规则适配（2026-10-26）

本分支在上游 `46c6d43` 基础上补齐 C/E 正文及引用上限、删除 E 模板凑字兜底、按新类别时限分配预算，并阻止超时后台写入覆盖返回答卷。验证范围和未完成项见 [当前规则与验证](docs/CURRENT_RULES.md)。下文 1.5.1 镜像、上传记录与得分是上游历史记录，不证明本分支已构建或通过平台评测。

## 官方基础镜像（已实测）

`agenthack/base:1.1.1` 已在本机导入并检查：

| 项 | 实测值 |
|---|---|
| Python | 3.11.16（不是 3.9；本地 3.9 代码保持兼容） |
| SDK | `agenthack-sdk 1.1.1` |
| 用户 | `agent`（uid 1000） |
| 工作目录 | `/app` |
| 端口 | 8080 |
| 健康检查 | `curl -fsS http://localhost:8080/health` |
| 预装 | fastapi 0.111、openai 1.35、httpx 0.27、pydantic 2.8、langchain |
| 数据 | `DATA_PATH=/data` |

**平台 HTTP 契约（实测）**：平台直接 POST 八字段题目 JSON 到 `/solve`，并直接读取五类答案 JSON。`agent.py` 的 `ContestRuntime` 保留 SDK 的 telemetry/session/task/tool/LLM，但替换掉 SDK 自带的 `/solve` 路由；`solve_task` 返回的 dict 原样作为响应，不再包 `TaskResponse`，也不再给 B/C/D/E 加 `answer`。

平台派题的 `constraints.timeout_ms` 不可依赖；`ContestRuntime` 用 `CONTEST_CASE_TIMEOUT_MS`（默认 300000）控制单题上限，D 类在内部再预留正式实验时间。

## 已构建的本地候选镜像

- `contest-agent:1.5.1` / `contest-agent.tar`（168 MB，linux/amd64，非 root，8080，保留 curl）。
- **不含 API Key**：`agent_config.json` 的 `api_key`/`base_url` 为空，`model` 为 `dsv4`；运行时由平台注入 `PLATFORM_LLM_URL` / `PLATFORM_API_KEY` / `AGENT_MODEL_NAME`。
- 标签：`org.agenthack.team_id=拿了国一她就会回来对不队`，`org.agenthack.version=1.5.1`。
- tar SHA-256：`9a71165baab4b1af22a9b429afe7bcb9ec02502f9d6d03b091e838ed149e826e`。
- 上一版保留为 `contest-agent-1.5.0.tar`；日常提交优先用 1.5.1 的 168 MB tar。
- 已用本地 mock 网关验证 A/B/D 链路；远程模型验证 A/B/C/E 与 D-01/02/05 生成；正式赛题 Gateway 仍未联调。

如果平台**不**注入 Key，需要明确授权把 Key 写入 `agent_config.json` 后重建；导出的 tar 会包含该 Key，不能公开。

## 两条运行路径

| 路径 | 入口 | 用途 |
|---|---|---|
| 官方评测 | `agent.py` + `ContestRuntime`（`agenthack/base:1.1.1`） | 提交镜像；SDK 提供 `/health`、telemetry、模型与工具采集，`/solve` 由赛事适配层接管 |
| 本地开发 | `app/main.py`（FastAPI） | 没有基础镜像时模拟同样的 `/health`、`/solve` 信封 |

本地 Python 3.9 只是为了和团队约定一致；官方镜像是 Python 3.11.16，代码保持 3.9 兼容即可两边都跑。

## 当前状态

- [x] `ContestAgent(BaseAgent)` + `AgentRuntime` 入口
- [x] 本地 `/health`、`/solve` 替身，模拟 `task_id/input -> status/output` 信封
- [x] `dispatch.json` 解析与 1-3-2-3-2 校验
- [x] A / B / C / D / E 五类 handler
- [x] C/E Markdown 命名、长度、引用、`## 参考文献` 校验
- [x] D 类 Gateway 生成 → 逐字保存 → 平台客户端 → 失败兜底
- [x] 结果写入 + 回读校验
- [x] 自定义工具统一走 `tools.py` 的 `@tool`
- [x] 官方基础镜像 `agenthack/base:1.1.1` 实测：`/health`、`/solve`、A/B/D 兜底全部通过
- [x] 本地 mock 网关 + 官方镜像：A/B/C/E/D 五类整链路通过，SDK 记录 llm_calls/tokens/tools
- [x] 远程模型真实联调：A/B/C/E 合规；D-01 3/3、D-02 3/3、D-05 7/7 一次通过
- [x] D 类公开样例自测 + 失败回修循环（真实 worker 子进程 + 协议校验）
- [x] C/E 接入 PaperCode 的 Gate-Honest / ClaimLedger / NoveltyCheckpoint 证据纪律
- [x] 40 个测试在 Python 3.9 下通过
- [x] 平台上传：`sub-20261026-0e295474.tar` READY，`/health` 检查通过
- [x] 首轮评测定位：平台直接发八字段题目 JSON，SDK 默认 `/solve` 的 `task_id/input` 信封导致 422；已换成赛题适配路由
- [x] 第二轮反馈：C/E 被 SDK 信封包裹且多 `answer`；team 与 D 客户端不一致；D 超时未写文件
- [x] 1.4.1 直接返回五类 JSON、统一 `RESEARCH_WORKSPACE_TEAM`、D 超时兜底
- [x] 1.5.1 D 预算：170s 案例预算、70s 正式实验预留、自测最多 2 个样例 × 8s；自测失败只在剩余充足时修复
- [x] 1.5.1 B：MaterialIndex + BM25 + 证据摘录；候选 15、最多返回 5、误报过滤
- [x] 1.5.1 C/E：ClaimLedger 证据闸门（未引用事实句 / 缺章节 / 占位 / 引用越界为 blocker），一次完整修复 + 摘录式兜底
- [x] 1.5.1 测试：46 个测试在 Python 3.9 下通过
- [ ] 重传 1.5.1 后重新评测
- [ ] 真实论文库/联调包测试

## 本地开发

Agent 服务与 D 类算法容器统一使用 **Python 3.9**（`.python-version` 锁定 3.9）。

```bash
cd ~/code/contest-agent
cp .env.example .env
uv sync --extra dev
uv run pytest
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

本地 `/solve` 与官方信封一致：

```bash
curl -s localhost:8000/health

curl -s localhost:8000/solve \
  -H 'Content-Type: application/json' \
  -d '{"task_id":"A-01","input":{"case_id":"A-01","function":"Chat","input":"解释光照与植物生长的关系","constraints":[]}}'
```

## 官方镜像

```bash
docker load -i agenthack-base-1.1.1.tar

cp agent_config.example.json agent_config.json
# 填入组织方提供的 base_url / api_key / model

./scripts/build_image.sh          # docker build --platform linux/amd64
docker save contest-agent:1.5.0 -o contest-agent.tar
```

`Dockerfile` 基于 `agenthack/base:1.1.1`，`USER agent`，`EXPOSE 8080`，`CMD ["python", "agent.py"]`。平台的工作目录是 **`/output`**，镜像里已设置 `CONTEST_WORKSPACE_ROOT=/output`；结果写到 `/output/data/<team>/`、`/output/data/expert/`、`/output/data/figure/<team>/`。额外依赖只装 `requirements.txt` 里的包；评测时不能联网安装。

### 模型配置的优先级

1. 运行时注入的 `PLATFORM_LLM_URL` / `PLATFORM_API_KEY` / `AGENT_MODEL_NAME`（优先）
2. 镜像内的 `/app/agent_config.json`（`base_url` / `api_key` / `model`）

仓库默认**不持久化真实 API Key**；本地测试用环境变量或 `.env`（已忽略）注入。平台指南同时提到“运行时优先注入”和“把 API Key 写入镜像”，两者冲突时以平台实际注入为准；如果平台不注入 Key，需要队长明确确认后再把 Key 写入 `agent_config.json` 并重建镜像。`agent_config.json` 已被 `.gitignore` 忽略。

## 目录

```text
agent.py          官方 SDK 入口（BaseAgent + AgentRuntime）
agenthack_compat.py  本地无 SDK 时的兼容 shim
tools.py          @tool 包装：PDF、检索、写文件、平台实验客户端、源码保存
app/              本地 FastAPI 替身（不进官方镜像）
core/             dispatch、schema、写入/回读、模型客户端、PDF/BM25/材料索引、solve_task
handlers/         A/B/C/E；D 委托给 d_adapter；evidence.py 是 ClaimLedger 证据闸门
d_adapter/        Gateway 生成、逐字保存、平台客户端、pipeline
tests/            mock LLM + schema + dispatch + source 提取
vendor/d-class/   平台公开 D 材料（已收录；不含算法）；vendor/reference/ 本地忽略
```

## D 类本地自测

```bash
# 用 D 包公开样例跑一份实现（真实 worker 子进程 + 协议校验）
uv run python scripts/d_selftest.py \
  --case D-01 \
  --implementation /path/to/implementation.py

# 默认材料在 vendor/d-class；也可以用 --materials / --samples 指定
```

在正式 `/solve` 里，D 适配器会先生成源码，再自动跑最多 2 个公开样例（每个默认 8 秒）；失败时把完整错误发回同一模型要求**完整修复版**，最多重试 `CONTEST_D_REPAIR_ATTEMPTS` 次（默认 1），全程受 170 秒案例预算和 70 秒正式实验预留约束。无论自测是否完成，只要已有完整源码就调用平台客户端换源码凭证。

官方镜像内置了公开样例（`/app/d-class-samples/公开样例请求`），所以评测时即使平台只挂载 case 目录，自测/回修循环仍可运行。

PaperCode 哪些部分被复用、哪些没有，见 [docs/PAPERCODE_REUSE.md](docs/PAPERCODE_REUSE.md)。面向专家复核的创新说明见 [docs/INNOVATION.md](docs/INNOVATION.md)。

## 比赛规则要点

- 11 题：A 1 / B 3 / C 2 / D 3 / E 2；自动榜满分 100。
- 结果写到 `data/<team>/<case_id>.json`；C/E 另存到 `data/expert/`；D 图表在 `data/figure/<team>/`。
- C/E 正文至少 800 字符，含 `## 参考文献`，JSON 的 `markdown` 与另存文件逐字一致。
- D 类三项指标、顺序和单位以本题 `protocol.json` 为准；`failed`/`materials_missing` 时三项 `observed` 全为 `null`。
- **D 类源码必须在本轮本题通过赛事 Gateway 现场生成、逐字保存，再用平台客户端换源码凭证执行。** 预置算法、本地改字、截断或多版本都会被拒绝。`vendor/reference/` 里的参考实现只用于本地自测，不能进镜像。
- 自定义 Tool 不放外网；模型调用只走赛事 Gateway；实际执行的工具函数必须带 `@tool`。
- 平台实际是逐题把八字段题目 JSON 直接 POST 到 `/solve`，响应也直接返回五类答案 JSON；SDK 的 `task_id/status/output` 信封只用于内部 telemetry，不能作为最终响应。

> 注意：官方开发指南的通用检查项写的是「`solve()` 返回结果包含 `answer`」，那是示例任务的字段。赛题 1 的五类输出 schema 不同（B 是 `papers`，C/E 是 `path` + `markdown`，D 是 `status` + `metrics` + `figures`），**以赛题 1 的冻结 schema 为准，不要给 B/C/D/E 加 `answer`**。`task_input` 的实际字段也要等赛题页/联调包的逐题输入格式确认。

## 分工

| 人 | 负责 |
|---|---|
| 陈加羽 | 架构、`agent.py`/`solve_task`、D 适配器、镜像、端到端 |
| 钟助辰 | D-01/02/05 参考实现（仅自测）、公开样例 expected、边界用例 |
| 李英浩 | A/B/C/E handler、schema、证据/引文、写入回读 |

## 协作

仓库：https://github.com/S0ra-ai/contest-agent

CI/CD 镜像构建：`.github/workflows/build-image.yml`，用法见 [docs/CI.md](docs/CI.md)。

先读 [CONTRIBUTING.md](CONTRIBUTING.md)。`vendor/d-class/` 是公开 D 材料，已提交；`vendor/reference/`、`agent_config.json`、`.env`、`*.tar` 不提交。
