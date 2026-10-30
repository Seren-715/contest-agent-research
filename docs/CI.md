# CI/CD 镜像构建

[`.github/workflows/build-image.yml`](../.github/workflows/build-image.yml) 做四件事：

1. 跑 `uv run pytest`（40 个测试）；
2. 加载 `agenthack/base:1.1.1`；
3. `docker build --platform linux/amd64`，创建**无 Key** 的 `contest-agent:<version>`；
4. 启动容器跑 `GET /health` 冒烟，导出 tar，上传 artifact；如果触发的是 tag，再创建 GitHub Release。

## 产物

- tag 构建的 Release asset 是 GitHub runner 直接 `docker save` 的**未压缩** tar（约 560 MB），`docker load -i` 可直接加载。
- 本机 Docker Desktop（containerd）导出的 OCI tar 约 169 MB，平台上传优先用这份；两者镜像 ID/content 一致，但 tar 哈希不同。
- 校验时以对应 `.sha256` 资产为准；不要拿本地 tar 的哈希去校验 Release 里的 tar。

## 触发方式

手动：

```bash
gh workflow run build-image.yml \
  --repo S0ra-ai/contest-agent \
  -f version=1.5.0
```

打 tag：

```bash
git tag v1.5.0
git push origin v1.5.0
```

tag 会同时生成 GitHub Release，tar 作为 release asset 下载；手动运行则在 Actions run 页面的 artifact 里下载（保留 14 天）。

## 一次性准备基础镜像

CI 不在仓库里存 175 MB 基础镜像，按下面优先级取：

1. `docker pull agenthack/base:1.1.1`（如果组织方把它发布到公开 registry）；
2. 私有 Release `base-image` 里的 `agenthack-base-1.1.1.tar`。

把本地基础镜像上传一次：

```bash
gh release create base-image \
  "/Users/s0ra/university/2026 第一届全国高校智能体网络创新挑战赛/agenthack-base-1.1.1.tar" \
  --repo S0ra-ai/contest-agent \
  --title "AgentHack base image 1.1.1" \
  --notes "Organizer-provided base image; private CI cache. Do not redistribute."
```

上传后，`build-image.yml` 的 `gh release download base-image` 会加载它。仓库是 private，release 也只有协作者可见。

如果你更想用 GHCR，也可以把基础镜像推到私有 package，然后在 workflow 里 `docker login ghcr.io` + `docker pull`；当前实现默认走 Release，少一个 packages 权限。

## 队伍标识

镜像 label 用仓库变量 `TEAM_ID`；不设置时默认中文队伍名：

```bash
gh variable set TEAM_ID \
  --repo S0ra-ai/contest-agent \
  --body "拿了国一她就会回来对不队"
```

运行时结果目录仍以平台注入的 `RESEARCH_WORKSPACE_TEAM` / `TEAM_ID` 为准。

## Key 与安全

- CI 创建的 `agent_config.json` 是**空 Key**：`{"base_url":"","api_key":"","model":"dsv4"}`。
- 正式评测由平台注入 `PLATFORM_LLM_URL`、`PLATFORM_API_KEY`、`AGENT_MODEL_NAME`。
- 不要把真实 Key 放进仓库变量、Actions secret 或镜像；当前流程不需要它。
- tar 是 keyless 的，可以作为 release asset 给协作者下载；平台上传仍由队长操作。

## 失败排查

- `agenthack/base:1.1.1 is unavailable`：创建上面的 `base-image` release，或确认公开 pull 可用。
- `/health did not become ready`：看容器日志；通常是 SDK/导入错误。
- `uv run pytest` 失败：先本地复现；CI 用 Python 3.9。
- Release 已存在：workflow 对同 tag 会 `gh release upload --clobber`。
