# 平台接入实测记录（agenthack/base:1.1.1）

记录日期：2026-10-22/23。基础镜像来源：`agenthack-base-1.1.1.tar`（168 MB，导入后 176 MB）。

## 0. 平台公开 API（2026-10-23 实测）

`GET http://8.149.132.48/api/v1/challenges/challenge-tbd` 返回：

- 提交物：Linux/AMD64、非 root、8080/tcp、`GET /health`、`POST /solve`。
- **工作目录 `/output`**；结果写 `data/<队伍标识>/`、`data/expert/`、`data/figure/<队伍标识>/`。
- 每道题接收完整题目 JSON，原样返回 `case_id`。
- 一轮 11 题（A/B/C/D/E = 1/3/2/3/2）。
- `limits.max_duration_ms = 5,400,000`（90 分钟）；`timeslice_budget = 300`。
- 平台只挂载允许的题目和论文材料。

`http://aiagent-anic.com/` 是同一个平台前端；`/api/v1/auth/me/team-key` 需要登录才能取本队 Key，公开接口不返回模型 Gateway 地址/模型名。

## 1. 镜像事实

| 项 | 实测值 |
|---|---|
| 镜像 tag | `agenthack/base:1.1.1` |
| Python | **3.11.16**（不是 3.9） |
| SDK | `agenthack-sdk 1.1.1` |
| 用户 | `agent`（uid 1000, gid 1000） |
| 工作目录 | `/app` |
| 端口 | 8080 |
| 健康检查 | `curl -fsS http://localhost:8080/health` |
| 预装 | fastapi 0.111、openai 1.35、httpx 0.27、pydantic 2.8、langchain |
| 环境变量 | `PLATFORM_LLM_URL`、`PLATFORM_API_KEY`、`TEAM_ID`、`DATA_PATH=/data`、`AGENTHACK_LOG_LEVEL`、`AGENTHACK_SDK_VERSION=1.1.1` |
| 默认 CMD | 故意失败，要求选手覆盖 |

基础镜像自带 `/app/src` 示例 Agent 和 `/app/requirements.txt`。我们的 Dockerfile 删除 `/app/src` 并覆盖 `requirements.txt`。

## 2. SDK 行为（读源码确认）

### `/solve`

```text
POST /solve
{"task_id": "A-01", "task_type": "qa", "input": {...}, "constraints": {"timeout_ms": 30000}}
-> {"task_id": "...", "status": "success|error|timeout", "output": {...}, "metadata": {...}}
```

`AgentRuntime` 只把 `request.input` 传给 `BaseAgent.solve(task_input)`。`task_id` 不在 `task_input` 里，但可以从 `agenthack.telemetry.current_session().attributes["task_id"]` 拿到。

### 输出强制 `answer`

`AgentRuntime._setup_routes` 的代码是：

```python
output = result if isinstance(result, dict) and "answer" in result else {"answer": str(result)}
```

也就是说：**返回 dict 没有 `answer` 时，SDK 会把整个 dict 转成字符串塞进 `{"answer": ...}`**。我们的处理：
- `solve_task` 在 `output` 信封里加一个 `answer` 摘要；
- 写盘的 `data/<team>/<case>.json` 仍然只有赛题字段；
- 这样 SDK 原样透传 structure，文件也不被污染。

### 超时

`Constraints.timeout_ms` 默认 **30000（30 秒）**，不是 180 秒。SDK 用 `asyncio.wait_for(..., timeout_ms/1000)` 包住 `solve()`。D 类生成 + 执行必须让平台把 timeout 设到 180000，否则会在生成阶段被 SDK 超时。**这是需要向组委会确认的第一优先级问题。**

### 工具采集

`@tool` 装饰器会记录 `tool_call_count`、`tools_used`、调用参数大小。我们实测 `/solve` 的 metadata：

```json
"tool_call_count": 1,
"tools_used": ["write_text_file"]
```

D 失败兜底时：

```json
"tool_call_count": 3,
"unique_tool_count": 2,
"tools_used": ["save_generated_source", "write_text_file"]
```

工具必须在 `AgentRuntime.solve` 会话内调用，否则 `tool_call()` 会抛 `RuntimeError`。我们的工具都在 `solve_task` 调用链内。

### 模型调用采集

SDK 拦截 `openai.resources.chat.completions.Completions.create`，记录 `llm_call_count`、input/output tokens、模型名、finish_reason。`core/llm.py` 已优先走 `openai.OpenAI`，本地没有 openai 时才退回 httpx。

## 3. 本次实测结果

构建：

```bash
docker build --platform linux/amd64 --build-arg TEAM_ID=smoke-team -t contest-agent:1.5.0 .
# 177 MB
```

运行：

```bash
docker run -d --name contest-smoke --platform linux/amd64 -p 127.0.0.1:8081:8080 \
  -e CONTEST_MOCK_LLM=1 -e CONTEST_TEAM=smoke-team -e TEAM_ID=smoke-team \
  -e AGENTHACK_TELEMETRY_REQUIRED=false contest-agent:1.5.0
```

A 类：

```json
{"task_id":"A-01","status":"success",
 "output":{"answer":"mock answer","case_id":"A-01","clarifying_question":"..."},
 "metadata":{"tool_call_count":1,"tools_used":["write_text_file"], ...}}
```

B 类（空论文库）：

```json
{"task_id":"B-01","status":"success",
 "output":{"answer":"检索到 0 篇相关论文","case_id":"B-01","papers":[]}}
```

D 类失败兜底（mock 模型，无法生成源码）：

```json
{"task_id":"D-01","status":"success",
 "output":{"answer":"实验状态：failed","case_id":"D-01","status":"failed",
   "metrics":[{"name":"Macro-F1","unit":"分数","reference":null,"observed":null,"relative_error":null}, ...],
   "figures":[]}}
```

写盘文件 `data/smoke-team/D-01.json` 只含 `case_id/status/metrics/figures`，三个指标对象齐全、`observed` 全为 null。

## 3.1 本地 mock 网关五类整链路（2026-10-23）

用 `scripts/mock_gateway.py` 起一个 OpenAI-compatible 本地网关，让官方镜像真实走 OpenAI 调用；用 `fpdf2` 生成 3 篇合成论文挂到 `/data/论文库`。结果：

| 类型 | 结果 | LLM 调用 | 工具采集 |
|---|---|---:|---|
| A | `answer` + `clarifying_question` | 1 | `write_text_file` |
| B | `papers` 只含真实文件名 | 2（查询扩写 + 重排） | `pdf_probe` / `read_materials` / `write_text_file` |
| C | 915 字 Markdown，JSON 与文件逐字一致 | 1 | `read_materials` / `write_text_file` |
| E | 921 字 Markdown，JSON 与文件逐字一致 | 1 | `read_materials` / `write_text_file` |
| D | 源码生成失败时诚实返回 3 项 null 指标 | 2（生成 + 修复） | `save_generated_source` / `write_text_file` |

`/solve` metadata 实测出现 `llm_calls`、`total_tokens`、`tools_used`，证明官方 SDK 的模型/工具采集链路可用。唯一未验证的是真实赛题 Gateway 的 URL/模型名。

## 3.2 真实远程模型联调（2026-10-26）

用一个 OpenAI-compatible 远程模型（`deepseek-v4-flash-vision-exp`，1M context，max output 384k）做提示词与生成质量验证；Key 只在命令环境变量里使用，没有写入文件或镜像。

| 类型 | 结果 |
|---|---|
| A | 完整回答 + 澄清问题 |
| B | 2 篇真实论文 + 具体匹配理由（查询扩写 + 重排） |
| C | 1744 字综述，引用齐全，JSON 与文件一致 |
| E | 3053 字 Idea 报告，含 NoveltyCheckpoint、互补关系、失败边界 |
| D-01 | 4473 字节源码，公开样例 3/3 一次通过 |
| D-02 | 8753 字节源码，公开样例 3/3 一次通过 |
| D-05 | 11885 字节源码，公开样例 7/7 一次通过 |

发现：推理型模型在 `max_tokens=16000` 时会被截断（`finish_reason=length`）；改为 64k 后一次通过。`CONTEST_D_MAX_TOKENS` 默认 64000，可用环境变量覆盖。

这是**远程测试模型**，不是正式赛题 Gateway；最终镜像仍是无 Key、运行时注入。

## 4. 仍需向组委会确认

1. **`timeout_ms`**：正式派题是否设置为 180000？默认 30 秒会让 D 类直接超时。
2. **判题读哪里**：判题读 `data/<team>/<case>.json`，还是 `/solve` 的 `output`？如果读 `output`，SDK 强制的 `answer` 字段是否会触发 schema 额外字段校验？
3. **`task_input` 映射**：赛题 1 的 `case_id/function/input/materials/constraints` 是整道塞进 `input`，还是只给 `question/context`？如果是后者，`case_id` 只能靠 `task_id` 兜底。
4. **`/solve` 调用次数**：11 道题是 11 次 `/solve`（`task_id` = case_id），还是 1 次 `/solve` 让我们读 `dispatch.json`？
5. **`TEAM_ID`**：正式环境注入的是平台分配 ID 还是中文队名？决定 `data/<team>/` 目录名和镜像 label。
6. **模型 Gateway**：`PLATFORM_LLM_URL` 和 `AGENT_MODEL_NAME` 的正式值；公开 API 不返回，需登录队伍账号查看或由组委会提供。
7. **API Key**：运行时注入优先，还是必须写入镜像？当前候选镜像不含 Key；如果平台不注入，需要明确授权后烘焙。

## 5. 构建/提交检查

- [x] 基于 `agenthack/base:1.1.1`
- [x] `linux/amd64`
- [x] `/health` 返回 `{"status":"healthy"}`
- [x] `/solve` 返回 `task_id/status/output`
- [x] `solve()` 返回结果含 `answer`
- [x] 自定义工具使用 `@tool`，metadata 有 `tools_used`
- [x] 离线依赖构建时安装
- [x] `USER agent`
- [x] `EXPOSE 8080`
- [ ] `agent_config.json` 填入真实 Gateway URL/Key/Model（当前是占位符）
- [ ] `TEAM_ID` 改成平台分配的正式 ID
- [ ] `docker save contest-agent:1.5.0 -o contest-agent.tar`

## 5. 平台上传实测（2026-10-26）

- 上传文件：`contest-agent.tar`（169 MB / 172642.5 KB）
- 平台 submission：`sub-20261026-0e295474.tar`
- 状态：**READY**；「镜像导入成功，结构与 /health 检查通过」；导入耗时 1 分 21 秒。
- 控制台提示：**「当前账号暂无评测提交权限，可先上传并检查镜像」**。这是账号/评测窗口权限，不是镜像问题；需要组委会或管理员开通 `can_submit_evaluations` / 确认评测开放时间。
- 控制台文案写「25 道独立科研任务（A–E 各 5 题）」，与 v3 文档的 11 题（1/3/2/3/2）不一致；Agent 按 `/solve` 的 case 逐题处理，不受总题数影响，但 D 类实际派哪几题需要确认（决定平台挂载哪些 `/data/experiments/<case_id>/`）。

### 5.1 首轮评测失败与修复（eval-20261026-14bd7b65）

平台反馈：

- `AGENT_RUNTIME_TRACE_MISSING`：未收到有效 AgentRuntime 追踪。
- 逐轮 A-03：`Agent 拒绝题目格式 (HTTP 422): 赛题一 /solve 必须直接接收八字段题目 JSON`。
- 模型调用 0 次、Token 0、模型耗时 0ms、纯处理 0ms。

根因：赛题平台把八字段题目 JSON **直接** POST 到 `/solve`；AgentHack SDK 1.1.1 默认 `TaskRequest` 要求 `task_id/input` 信封，FastAPI 在路由校验阶段就 422，`solve()` 根本没执行，所以也没有 telemetry/trace。

修复（`agent.py` 1.3.0）：

- 新增 `ContestRuntime`，移除 SDK 默认 `/solve`，换成接收任意 JSON 对象的赛题路由。
- 仍然使用 SDK 的 `Telemetry.session()`、`task()`、`@tool`、OpenAI 拦截器和 `flush()`。
- 返回标准 `TaskResponse`：`task_id/status/output/metadata`；`output` 是五类赛题结果，`metadata` 含 `session_id/trace_id/llm_calls/total_tokens/tools_used`。
- 单题 timeout 默认 180000ms，可用 `CONTEST_CASE_TIMEOUT_MS` 覆盖。
- 官方镜像内实测：八字段 A 题直接 200，结果文件正确，metadata 有 session/trace。

首轮评测的 A-03 题目本身是八字段 JSON；修复后应通过路由校验并进入 solve。若仍报 `AGENT_RUNTIME_TRACE_MISSING`，则更像平台 OTLP 采集链路问题，需要评测编号请管理员核查。

### 5.2 第二轮评测失败与修复（eval-20261026-3c044536）

逐题结果：

- A-02 / B-01..03：选手 `/solve` 接口调用未成功。
- C-01/C-03/E-01/E-02：输出字段结构错误（缺必填字段/类型不符）。
- D-01/D-02：实验证据校验失败（回执/哈希/队伍/批次/运行编号）。
- D-05：结果文件缺失。

根因与修复（1.4.0）：

1. **响应信封错误**：1.3.0 返回 SDK `TaskResponse`（`task_id/status/output/metadata`），赛题判题器直接读响应体，C/E 看到的是信封而不是五类 JSON；B/C/D/E 还多了 SDK 需要的 `answer`。1.4.0 直接返回五类 JSON，B/C/E/D 不再有 `answer`，结果文件保持严格 schema。
2. **team 不一致**：`data/<team>` 用 `TEAM_ID`，D 客户端用 `RESEARCH_WORKSPACE_TEAM`，可能导致 D 证据队伍/路径校验失败、D-05 文件找不到。1.4.0 优先用 `RESEARCH_WORKSPACE_TEAM`。
3. **D 超时无兜底**：`TimeoutError` 没被 `solve_task` 捕获，D-05 可能因此没有写结果文件。1.4.0 捕获 `TimeoutError`，写三项 null 的合法 failed 答卷。

官方镜像内 1.4.0 直返测试：A 返回 `case_id/answer/clarifying_question`；B 返回 `case_id/papers`；C/E 返回 `case_id/path/markdown`（fallback 815/809 字，JSON 与文件一致）；D 返回 `case_id/status/metrics/figures`；结果目录为 `data/<RESEARCH_WORKSPACE_TEAM>/`。

如果 A/B 仍服务失败，优先查平台模型注入/HTTP 超时；如果 D 仍证据失败，查平台源码凭证/队伍/批次。两者都需要新 run detail 的逐题模型调用和错误码。

### 5.3 1.4.1：D 自测强制标准库

`d_adapter/selftest.py` 现在用 `python -S` 跑 D worker，禁用 site-packages，模拟平台 Python 3.9 标准库算法容器。这样如果生成代码 import 了第三方库，会在自测阶段暴露并触发完整修复，而不是到平台执行才失败。D-01/02/05 参考实现仍为 3/3、3/3、7/7。

### 5.4 第三轮真实评测与 1.5.0（eval-20261026-06b391f3）

得分：

- A-04：5 / 5
- B-01/B-02/B-03：3.75 / 2.25 / 3.75（满分 15）
- C-01/C-03：7.3 / 7.3（满分 25）
- D-01：**40 / 40**
- D-02/D-05：0，错误「只生成源码或进行本地自检，没有调用平台正式实验入口」
- E-01/E-02：0，无可见错误提示

结论与修复（1.5.0）：

1. **D-01 满分证明主链路正确**：Gateway 生成、`-S` 标准库自测、平台客户端换证、正式执行、结果落盘全部通过。
2. **D-02/D-05 没进正式实验**：180 秒预算被生成/自测/修复吃掉，`deadline.require(3)` 之前就返回了。1.5.0：案例预算 300 秒、正式实验预留 70 秒、自测到预留线即停、最多 1 次修复、**无论自测是否通过都调用平台客户端**。D-05 本地用真实模型验证：8.5 KB 源码、7/7 公开样例通过、正式入口调用时仍预留 109 秒。
3. **E 两个 0**：E 的校验错误地把「必须引用每一篇指定论文」套到了 Idea；模型只引用部分论文时被降级成「受限运行」报告，coverage 为 0。1.5.0：E 只要求至少一条可追溯引用 + 参考文献里至少一个真实文件名，C 仍要求全部指定论文。
4. **B 偏低**：模型过于保守，只回 1-2 篇，漏掉相关论文。1.5.0：候选池扩到 15、最多返回 6 篇、覆盖至少两个检索要素的部分相关论文也要列出。
5. **C 偏低**：单篇上下文从 6000 扩到 8000 字符，并要求一张「研究问题/方法/数据/指标/结论/局限」证据表，表内事实仍用 [编号] 支撑。

### 5.5 1.5.1：D 预算与 PaperCode 证据闸门

第四轮评测（eval-20261026-8f1a1173）总分不变：A 5、B 3.25 平均、C 7.3 平均、D-01 40、D-02/05 仍 0、E 仍 0。结论与修复（1.5.1）：

1. **D-02/D-05 仍未进正式实验**：1.5.0 的 300 秒案例预算超过平台 180 秒口径；D-05 自测 7 个样例 × 20 秒会吃光预算。1.5.1：案例预算 170 秒、正式预留 70 秒、自测最多 2 个样例 × 8 秒，失败后只在剩余 > 115 秒时修复，否则直接调用平台客户端。D-01/02/05 参考实现 2 个样例均在 0.25 秒内通过。
2. **E 仍为 0**：1.5.0 的 E 校验放宽后，模型输出仍可能因缺章节/缺引用被降级成内容为空的「受限运行」报告。1.5.1：接入 PaperCode 的 ClaimLedger/章节闸门，缺少 E 的 9 个必需小节或事实句无引用都是 blocker；一次完整修复后，兜底报告改为逐篇引用摘录的实质报告。
3. **C 7.3 的引文缺口**：C 现在逐篇检查正文引用 + 参考文献文件名，缺哪篇就补哪篇的摘录段；引用覆盖不再依赖模型是否记得列全。
4. **B 偏低**：材料索引 + BM25（标题加权）替代裸词频；候选池 15，模型最多返回 5 篇，只共享泛词的结果会被过滤。
5. **Token 变高**：D-02/D-05 的重复生成/修复被预算截断；D 只在剩余充足时修复一次，正式实验优先。

本地未验证项：新镜像尚未重传，正式 Gateway 的 180 秒行为仍需下一轮评测确认。
