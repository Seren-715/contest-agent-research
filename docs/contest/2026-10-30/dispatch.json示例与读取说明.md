# dispatch.json 示例与读取说明
一轮 11 题，A/B/C/D/E 各 1/3/2/3/2 题；逐题输入和答案字段不变。

完整结构示例见 [dispatch.example.json](示例/dispatch.example.json)。其中 example-team 是演示工作区标识；示例不会创建真实队伍目录，不是可替代正式派题记录的提交文件。正式读取平台提供的记录，不自行修改队伍、题号、题目摘要或配置摘要。

| 类别 | 数量 | 当前派题方式 |
|---|---:|---|
| A / Chat | 1 | 从已配置的 A 类题池随机抽取 |
| B / Literature_Retrieval | 3 | B-01、B-02、B-03 |
| C / Survey | 2 | C-01、C-03 |
| D / Reproduction | 3 | D-01（easy）、D-02（medium）、D-05（hard） |
| E / Idea | 2 | E-01、E-02 |

以上固定题号和 D 难度分级是当前轮次配置；所有队伍的 B–E 题目相同。难度表示赛事调度分级，单题评分标准不变。

`case_ids` 是本轮全部题号；`cases[].question_file` 是相对工作区根目录的题目路径。`cases_per_category` 是 A–E 数量对象，不再是单个整数。`dispatch_policy_version` 和 `dispatch_policy_sha256` 标识本轮派题配置；`question_sha256` 标识题目文件内容；D 条目的 `difficulty` 与顶层 `difficulty_by_case` 一致。

本地文件模式按以下顺序执行：

1. 读取 `data/<本队工作区标识>/dispatch.json`，核对队伍、11 个唯一题号及各类数量。
2. 按 `cases[].question_file` 读取当前题目，核对内部 `case_id` 和 `function`。
3. 按题目允许材料定位论文与实验材料，逐题生成答案。
4. 将 11 个结果 JSON 写入 `data/<本队工作区标识>/<case_id>.json`，C/E 各另存两份同文 Markdown，D 图表按各题要求保存。
5. 回读文件，确认题号、正文和路径一致；不提交未派题号或给结果添加评分字段。

外部平台若以 HTTP 逐题发送八字段题目 JSON，是否提供派题文件与实际挂载位置以该平台协议为准；一次请求只处理当前题，Agent 不负责抽题。输出 Schema 见 [Agent输入输出格式规范.md](Agent输入输出格式规范.md)。
