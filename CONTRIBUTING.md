# 协作指南

## 环境

- Python **3.9**（`.python-version`），用 `uv` 管理依赖。
- 基础镜像 `agenthack/base:1.1.1`；最终镜像必须 linux/amd64、非 root、8080、保留 curl。

```bash
cd ~/code/contest-agent
uv sync --extra dev
uv run pytest -q
```

## 常用命令

```bash
# 全部测试
uv run pytest

# D 类公开样例自测（需要一份本地实现；仓库不提交预写算法）
uv run python scripts/d_selftest.py --case D-01 --implementation /path/to/impl.py

# 本地提交结构检查
uv run python scripts/validate_local.py --team <队伍标识> --root .

# 真实 Gateway 探活（环境变量见 .env.example）
uv run python scripts/check_gateway.py

# 本地 mock 网关（测试用）
python3 scripts/mock_gateway.py --port 18090

# 构建/导出镜像
TEAM_ID="<平台队伍标识>" AGENT_VERSION="<版本>" ./scripts/build_image.sh
docker save contest-agent:1.5.0 -o contest-agent.tar
```

## 不要提交

- `agent_config.json`、`.env`：真实 API Key / Gateway 地址只在本地。
- `*.tar`：最终镜像 tar 169 MB，GitHub 单文件限制；用网盘/平台共享。
- `vendor/reference/`：本地 D 参考实现，属于预写算法，不能进镜像或仓库。
- `data/`、`runs/`、`.venv/`、`__pycache__/`。

`vendor/d-class/` 是公开的 D 材料，已提交；它只有空 `implementation.py` 桩，不含算法。

## 分支与评审

- `main` 保持可运行；功能用 `feat/<topic>`，修复用 `fix/<topic>`。
- 提交前跑 `uv run pytest -q`；CI 会在 PR 上跑同一套测试。
- 一个人负责 D 算法验证，一个人负责 A/B/C/E 输出与证据，队长负责 `/solve` 适配、镜像和端到端。详见 README 分工。

## 当前关键约束（来自平台真实反馈）

1. 平台 `/solve` **直接接收八字段题目 JSON**，响应也**直接返回五类答案 JSON**；不要包 SDK `TaskResponse`。
2. B/C/D/E 不能有多余顶层字段（尤其不要加 SDK 的 `answer`）。
3. 结果目录必须与 D 客户端一致：优先 `RESEARCH_WORKSPACE_TEAM`。
4. D 源码必须当前 `/solve` 会话通过 Gateway 生成、逐字保存、自测/修复后交平台客户端；本地参考实现只用于自测。
5. 1.4.1 已修 `/solve` 契约、直返 JSON、team 路径、D 超时兜底，并用 `python -S` 强制 D 自测标准库。

## CI/CD

- `test` workflow：push/PR 跑 40 个测试。
- `build-image` workflow：手动或 tag `v*` 触发；先跑测试，再加载 `agenthack/base:1.1.1`，构建 linux/amd64 镜像，`/health` 冒烟，导出 tar，上传 artifact；tag 还会创建 GitHub Release。
- 基础镜像一次性放私有 Release `base-image`；详见 [docs/CI.md](docs/CI.md)。
- CI 构建的 `agent_config.json` 是空 Key；平台评测时注入 Gateway。

## 文档

- 平台实测与错误分析：[docs/PLATFORM.md](docs/PLATFORM.md)
- 提交 runbook：[docs/SUBMIT.md](docs/SUBMIT.md)
- PaperCode 复用：[docs/PAPERCODE_REUSE.md](docs/PAPERCODE_REUSE.md)
- 专家复核材料：[docs/INNOVATION.md](docs/INNOVATION.md)
- D 参考实现说明：[docs/D_REFERENCES.md](docs/D_REFERENCES.md)
