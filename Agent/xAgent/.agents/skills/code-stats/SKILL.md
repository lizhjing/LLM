---
name: code-stats
description: 统计当前项目的代码规模(文件数、总行数)。当用户要求"统计代码量/项目规模/代码行数"时使用,需配合 RunCli 与 getDate 工具完成。
---

# 项目代码统计规范

按以下步骤完成统计,所有数据必须来自工具的真实输出,禁止编造:

前提:需先设置 `XAGENT_CLI_ALLOWLIST=python` 并重启 Agent,否则 RunCli 会返回白名单拒绝。

1. 使用 RunCli 工具执行一行式 Python 命令,统计当前目录下 Python 源码的
   文件数与总行数(排除 __pycache__ 目录),例如:
   - command: `python`
   - args: `["-c", "import pathlib; fs=[p for p in pathlib.Path('.').rglob('*.py') if '__pycache__' not in p.parts]; print(len(fs)); print(sum(len(p.read_text(encoding='utf-8', errors='ignore').splitlines()) for p in fs))]"`
   该命令第一行输出文件数,第二行输出总行数。
2. 使用 getDate 工具获取统计时间。
3. 严格按以下格式输出(不要添加其他段落):

```
【代码统计报告 | <getDate 返回的日期>】
- Python 文件数: <N>
- 总行数: <N>
- 解读: <一句话说明规模量级>
```
