---
name: officecli
description: "调用本地 officecli.exe 命令行读取、查看、编辑 Word(docx)/Excel(xlsx)/PPT(pptx) 文件。当用户要求读取文档内容、创建或修改 Office 文档、提取正文或表格时,必须先加载本技能获取正确的命令语法,避免参数错误浪费调用轮数。"
---

# officecli 使用指南

officecli 是 AI 友好的 Office 文档命令行工具。**每条命令都必须带文件路径**;全局选项只有 `--json` 和 `--help`,不要发明其他标志。

## 读取文档内容(按优先级)

```bash
# 1. 读正文文本(首选,紧凑可靠;可配 --start/--end/--max-lines 控制范围)
officecli view <file> text --max-lines 120

# 2. 看文档结构大纲(标题层级)
officecli view <file> outline

# 3. 取单个节点(路径含 [] 必须加引号)
officecli get <file> "/body/p[1]"

# 4. CSS 式批量查询元素
officecli query <file> "p"

# 5. 统计信息 / 问题检查
officecli view <file> stats
officecli view <file> issues
```

view 的 mode 取值:`text` `annotated` `outline` `stats` `issues` `html` `svg` `screenshot` `pdf` `forms`(没有 `plain`/`table`/`title` 等)。

## 关键禁忌(历史踩坑,每条都真实发生过)

- **不要用 `get <file> /` 拉全文**:返回的是文档元数据(主题色、兼容性等几十个字段),不是正文,且超长被截断后正文完全丢失。
- **不要发明标志**:`--read`、`--body` 等不存在;`get <file> --body` 会把 `--body` 当作文档内路径报错。
- **不要丢文件名**:`get /body` 会被当成磁盘路径(报 File not found),即使已 `open` 常驻也要每条命令带文件。
- **属性一律用 `--prop 键=值`**:位置参数传键值会报 "No properties to set";裸写 `--text "..."` 会被静默忽略(产生空段落)。
- **add 必须指定类型**:`add <file> <parent>` 不带 `--type` 会报 "Either --type or --from must be specified"。
- **输出超长会被截断**:宁可多次精确取(`/body/p[1]`、`--start/--end` 分段),不要一次拉全量 JSON。
- **不要反复跑 `--help`**:输出数千字符且被截断,本技能已含所需全部语法。
- **出错重试前先核对文档**:`add` 失败后重试前,先 `view <file> text` 检查已有段落(失败的调用可能已产生段落,`\n` 会拆成多段),避免写出重复内容;收尾前 view 核对一遍再 save。
- **文件被守护进程占用先 close**:officecli 有常驻守护进程(空闲 2-10 秒自动落盘)。外部删除/覆盖文件前先 `officecli close <file>`;若出现"create 说文件存在但磁盘上没有"的矛盾状态,先关文档或结束 officecli 进程,否则守护进程会把陈旧状态刷回磁盘。

## 创建与编辑文档

```bash
officecli create <file> --type docx          # 新建文档(同时驻留内存)
officecli add <file> /body --type paragraph --prop text="第一段"
officecli set <file> "/body/p[1]" --prop text="新文本"
officecli remove <file> "/body/p[2]"
officecli save <file>   # 落盘并保持常驻;或 close <file> 落盘并退出常驻
```

- 文本中 `\n` 分段、`\v` 段内换行。
- 批量操作用 `batch <file>`(JSON 数组,每项 `{"command":"add","parent":"/body","type":"paragraph","props":{"text":"..."}}`)。
- 改完务必 `save` 或 `close`,否则外部程序读到的还是旧文件。

## 能力速查

- 不知道元素/属性名:`officecli help docx`(元素列表)、`officecli help docx <element>`(详情);格式有 `docx` `xlsx` `pptx`。
- 原始 XML 兜底:`raw` / `raw-set`。
- 退出码:`0` 成功,`1` 失败(stdout 是 JSON 信封,读 error 字段),`2` 成功但有警告。
