# 1.5.5 历史调试镜像分卷

本目录是历史镜像 `contest-agent-1.5.5-real-debug.tar`，不是最新比赛镜像。下载 `part001` 至 `part004` 和 `merge_parts.py` 到同一文件夹，然后运行：

```powershell
python merge_parts.py
```

脚本生成 `contest-agent-1.5.5-real-debug.tar`，并核对 SHA-256。已存在同名文件时不会覆盖。校验值：`528bf9b9dddd6ac9bef801e4e4249bdea258ee6d2faedd99519e8f1105832374`。

完整镜像也可从 [Release](https://github.com/Seren-715/contest-agent-research/releases/tag/v1.5.5-real-debug) 下载。镜像未经过当前平台正式评测。
