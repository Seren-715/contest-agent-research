# 提交 Runbook

本分支先按 [当前规则与验证](CURRENT_RULES.md) 验收；下列旧版本镜像记录保留作历史，不能代替本分支作品。

## 0. 平台事实（2026-10-23 实测）

- 提交物：Docker/OCI tar（`.tar` / `.tar.gz` / `.tgz`），基于 `agenthack/base:1.1.1`。
- 镜像要求：`linux/amd64`、非 root、声明 `8080/tcp`、保留 `curl`。
- 接口：`GET /health`、`POST /solve`。
- 工作目录：`/output`；结果写 `/output/data/<队伍标识>/`、`/output/data/expert/`、`/output/data/figure/<队伍标识>/`。
- 平台会先导入镜像并探活，通过后自动评测；同一队伍同时只能有一条评测。
- 超时逐轮判定；纯处理时间与模型调用时间分开统计。
- Token 用量与专家复核独立展示，不进入自动榜。

## 1. 构建前

```bash
cd ~/code/contest-agent
uv sync --extra dev
uv run pytest -q
```

确认 `vendor/reference` 没有被复制进镜像（Dockerfile 只复制 `公开样例请求`）。

## 2. 填入真实配置

优先用运行时环境变量，不要把真实 Key 提交到 git：

```bash
export PLATFORM_LLM_URL="组织方提供的模型接口地址"
export PLATFORM_API_KEY="本队 API Key"
export AGENT_MODEL_NAME="组织方提供的模型名称"
```

如果平台确认**不会**在运行时注入 Key，再把它写入 `agent_config.json`（已忽略）并重建镜像；这需要队长明确同意，因为导出的 tar 会包含该 Key。

`agent_config.json` 已被 `.gitignore` 忽略。镜像若包含 API Key，**不要公开 tar 或仓库**。

## 3. 构建镜像

```bash
TEAM_ID=<平台分配队伍标识> AGENT_VERSION=<版本号> ./scripts/build_image.sh
```

当前已构建的本地候选（不含 Key，运行时注入）：

```text
contest-agent:1.5.1        176.1 MB (docker image inspect)
contest-agent.tar          168 MB
SHA-256                    9a71165baab4b1af22a9b429afe7bcb9ec02502f9d6d03b091e838ed149e826e
LABEL org.agenthack.team_id=拿了国一她就会回来对不队
LABEL org.agenthack.version=1.5.1
```

上一版镜像保留为 `contest-agent-1.5.0.tar`；1.5.1 的 tar 是当前提交候选。

如果平台不注入 API Key，需要队长明确同意后再把 Key 写入 `agent_config.json` 并重建；tar 会包含 Key。

检查：

```bash
docker run --rm --platform linux/amd64 --entrypoint /bin/sh contest-agent:1.5.0 -c \
  'id; python --version; curl --version | head -1; ls /app'
```

应看到 `uid=1000(agent)`、Python 3.11、curl、`agent.py`、五类目录。按 2026-11-01 D 类更新通知，镜像不再内置练习题样例（`/app/d-class-samples` 不应存在）。

## 4. 本地冒烟

```bash
docker run -d --name contest-smoke --platform linux/amd64 -p 127.0.0.1:8080:8080 \
  -e CONTEST_MOCK_LLM=1 -e CONTEST_TEAM=smoke-team -e TEAM_ID=smoke-team \
  -e AGENTHACK_TELEMETRY_REQUIRED=false \
  -v /tmp/contest-output:/output \
  contest-agent:1.5.0

curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/solve \
  -H 'Content-Type: application/json' \
  -d @/tmp/solve-body.json
```

确认 `/tmp/contest-output/data/smoke-team/A-01.json` 存在且只有赛题字段。

## 5. 导出与上传

```bash
docker save contest-agent:1.5.0 -o contest-agent.tar
ls -lh contest-agent.tar
```

在平台「参赛中心」上传 tar；平台导入并探活，通过后自动评测。失败时看 `ERR_*` 错误码和容器日志。

## 6. 提交前检查

- [ ] `/health` 返回 `status: healthy`
- [ ] `/solve` 接收八字段题目 JSON，直接返回对应五类答案，不包 `task_id/status/output`；B/C/D/E 不额外添加 `answer`
- [ ] 结果文件在 `/output/data/<team>/<case_id>.json`
- [ ] C/E Markdown 在 `/output/data/expert/` 且 JSON 逐字一致
- [ ] D 图表在 `/output/data/figure/<team>/`，三项指标齐全
- [ ] D 源码是本轮 Gateway 现场生成，未本地改字
- [ ] 镜像 linux/amd64、非 root、8080、curl
- [ ] API Key 未提交到 git；运行时注入，或经确认后写入镜像
- [ ] A/B 外层≤540秒、C/D/E≤900秒；D客户端≤880秒，且所有阶段共享本题剩余预算
- [ ] C 正文≤5000字符、引用组≤40，E正文≤4500；失败不以通用模板或补字冒充完成

## 7. 脚本发起评测（`scripts/submit_eval.py`）

平台接口没有公开文档，本仓库也连不上平台，所以脚本不猜接口，只重放浏览器里那条真实的「发起评测」请求。

1. 在网页上照常上传 tar，等状态变成 READY。DevTools 复制上传请求时会去掉文件内容，所以上传这一步不能用脚本重放。
2. 打开 F12 → Network，点「发起评测」，在列表里找到这条请求，右键 → Copy → **Copy as cURL (bash)**，存成仓库外的文件，例如 `~/eval.curl`。文件里有登录 Cookie，不要提交或外传（仓库已忽略 `*.curl`）。
3. 先检查脚本能不能解析，这一步只解析、不发请求：`python3 scripts/submit_eval.py ~/eval.curl --dry-run`
4. 正式运行：`python3 scripts/submit_eval.py ~/eval.curl --interval 60 --max-minutes 120`

判定规则：

- 2xx 且响应里有 `eval-YYYYMMDD-…` 编号，没有忙碌字样 → 成功，打印编号后退出。
- 429/409/5xx，或响应里有"排队、评测中、稍后"等字样 → 按间隔重试。重试间隔最小 20 秒，带 ±随机抖动，并遵守 `Retry-After`。
- 401/403 → 登录过期，退出。
- 其他响应，或同时有编号和忙碌字样 → 停止，不盲目重放有副作用的请求。看清响应后，可以用 `--ok-pattern` / `--busy-pattern` 调整判定。

Cookie 和 Token 经 stdin 传给 curl，不出现在命令行里，日志中也会脱敏。
