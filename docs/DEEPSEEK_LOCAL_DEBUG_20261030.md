# DeepSeek真实调用与docker日志排查（2026-10-30）

## 测试边界

用户提供本地DeepSeek Key并授权本地真实模型测试。没有平台E-01原题及指定论文，使用公开示例形式的E-99请求与合成PDF；不能把这次结果当作平台同题复现或正式分数。Key仅只读挂载到测试容器后由进程读取，未写入产品源码/镜像或日志。

接口与模型名依[DeepSeek官方当前文档](https://api-docs.deepseek.com/en/)使用 `https://api.deepseek.com` / `deepseek-flash`。原基础镜像有空URL环境变量，本地启动器显式补充默认URL/模型；首次配置检查未触发模型请求，单独保留，不能误记成API失败。

## 实测失败

在未修改的 `contest-agent:1.5.4-format`、真实SDK1.1.1容器里，DeepSeek调用两次，均finish_reason=stop：

| 项目 | 首稿 | 完整修复稿 |
| --- | ---: | ---: |
| 总Token | 5676 | 5466 |
| content字符 | 2555 | 2673 |
| 模型请求耗时 | 36.93秒 | 15.87秒 |
| 本地unsupported_claim阻断 | 6 | 2 |

请求最终HTTP503，52.92秒。修复稿schema通过，正文2456字符，参考节前含标题2570字符，未超4500限制。

docker logs明确记录：

```text
report_rejected case_id=E-99 function=Idea categories=['unsupported_claim']
case_failed case_id=E-99 function=Idea error_type=SolveError cause_type=HandlerError
```

被拒的真实句子包括“实验拟设置以下基线（均为待执行计划，不是已有实验）”以及“可证伪预测拟为…”。这证明当前本地路径仍把研究设计关键词当成已有事实；不是本次真实模型的JSON字段或长度问题。平台E-01是否同因仍缺实际响应证明。

## 修复

Idea的审查分为：明确已有论文归因/已报告结果缺引用仍阻断；仅数字、指标词命中的不确定表述为 `claim_semantics_unverified` warning。停止靠追加句首“计划”正则枚举所有方案写法。warning不表示事实已受原文支持；模型生成提示、真实引用、长度/路径校验、非法编号检查和Survey规则保留。

两份原始真实模型文本原样回放，通过审查，没有拼接/截断或虚挂引用。增加独立摘句回归，使CI不依赖本地API响应文件；完整原稿测试若本地文件不存在则明确跳过。

为控制用量，修复后容器验证重放已保存的真实响应，不再发付费请求。验证原始文本只做现有末尾换行规范化，核对HTTP200、JSON与文件一致。不是一次新的在线生成成功证明。

本次付费调用总计2次、11142 Token；未运行批量评测，未调用赛事网关。正式提交仍使用赛事网关，DeepSeek Key不得写入作品镜像。
