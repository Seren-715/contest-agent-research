# D 参考实现说明

`vendor/reference/D-01.py`、`D-02.py`、`D-05.py` 是本地自测用的参考实现，**不提交到仓库、不进镜像**。

原因：

- 赛题要求 D 类正式源码必须在本轮本题通过赛事 Gateway 现场生成、逐字保存，再交平台客户端换源码凭证执行；
- 仓库或镜像里的预写算法会被来源准入拒绝，也可能被认定为绕过要求。

用法：

```bash
# 只在本地存在 vendor/reference/ 时
uv run python scripts/d_selftest.py --case D-01 --implementation vendor/reference/D-01.py
```

仓库的自动化测试使用脚本化的假模型响应，不需要参考实现；CI 也不会下载或提交它们。如果队友需要参考实现，请私下共享，并继续保持在 `vendor/reference/`（已被 `.gitignore` 忽略）。

D 自测现在用 `python -S` 运行，禁用 site-packages，模拟平台 Python 3.9 标准库算法容器；生成代码如果 import 第三方库会在自测阶段暴露并触发完整修复。
