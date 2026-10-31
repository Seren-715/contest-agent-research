# contest-agent-research

科研助手比赛Agent的独立研究与改进仓库。源码快照来自用户fork `Seren-715/contest-agent` 的本地修复分支，版本 `5937be21d7f4f6a9d8c78b513c31a4c767e3a133`（镜像1.5.5）。保留上游 `S0ra-ai/contest-agent` 来源、第三方声明和公开D脚手架。

## 从这里开始

1. **[深度研究任务说明](RESEARCH_BRIEF.md)**：结果、证据边界、研究优先级与可直接使用的提示词。
2. **[比赛材料索引](docs/contest/README.md)**：日期归档的公开规范、评分、派题说明与D要求。
3. **[完成工作与验证](docs/WORK_COMPLETED.md)**：修复沿革、真实调用与回放、测试及镜像。
4. [测试成绩转录](docs/evidence/platform-score.json)：截图自动59.4/100，测试评测不进公开榜，专家待复核，不是最终综合成绩。

代码入口：`agent.py`、`core/`、`handlers/`、`d_adapter/`。`vendor/d-class`含公开方法、协议、样例和空实现，不含可预置提交的完整算法。

## 当前状态

- 1.7.0平台测试58.4/100：B三题均在模型调用前异常，C事实0/15、引用6.5/10，D 36.4/40，E 10.5/15。平台未提供原题、答卷或诊断日志，无法确定B异常根因。
- 最新本地候选改进B的PDF兼容与短理由保留，C结合查询摘录和多维原文窗口，并检查引用编号和文件名的对应关系。Python 3.9/3.11各232通过、3跳过；官方SDK离线容器使用合成论文和本地模型桩验证B/C接口。未完成新一轮平台评测，不证明B异常已解决或分数提高；更多上下文可能增加token和耗时。
- 1.7.0修复描述性材料、辅助文件和重复挂载兼容；详见[恢复说明](docs/RECOVERY_1_7.md)。

- B/C新增有界页段索引、材料范围检查、分批检索与六维证据矩阵；实现、配置、离线评估和验证边界见[本轮优化说明](docs/BC_OPTIMIZATION.md)。本轮双版本182通过、3跳过，尚未平台复测。

- 本地Python3.9.25/3.11.15各146通过、1跳过；缺vendor/reference项跳过。两条真实输出回归依赖未上传的本地响应文件，在新仓库会另行明确跳过，独立摘句回归保留。
- DeepSeek两次真实生成复现E99的HTTP503：研究设计被unsupported_claim误拦；修复后原始真实响应回放HTTP200并核对文件一致。使用合成论文，不是平台E01原题，也不是新一次在线生成成功证明。
- 截图B0.8/15、C7.8/25（事实0/15、引文7.8/10）是研究重点；D35.6/40优先保留。截图确切镜像/评测ID未绑定。

## 验证与构建

```bash
uv sync --extra dev
uv run pytest -o addopts= -q -ra
```

默认本地Python3.9，支持3.9～3.11；CI矩阵覆盖两版本。官方主容器Python3.11.16，D算法环境Python3.9。

镜像基于组织方 `agenthack/base:1.1.1`、linux/amd64、非root、8080。`agent_config.json`在本地创建，正式使用赛事网关注入配置，不能将真实Key写入源码或作品镜像。镜像tar不在Git，见[1.5.5校验记录](docs/evidence/image-1.5.5-verification.json)。构建工作流需要另取得官方基础镜像，并非本仓库开箱构建。

## 来源

这是独立仓库，不是GitHub fork，不向上游创建PR。见[来源记录](docs/PROVENANCE.md)与[NOTICE](NOTICE.md)。[上游历史README](docs/UPSTREAM_README_HISTORICAL.md)中的镜像、队伍、成绩与未完成项是历史叙述，当前状态以本入口及实际证据为准。

未上传API Key、dotenv、带凭据配置、私有模型原始响应、论文、截图个人信息或镜像tar。公开材料版权归发布者，vendor保留原许可证与声明。
