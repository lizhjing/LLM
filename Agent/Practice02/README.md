# Practice02 — 带 Function Calling 的多轮流式对话

基于 Practice01 的流式多轮对话,新增 **Function Call(函数调用)** 能力:LLM 可根据用户意图,自动调用 13 个本地工具(日期计算 + 文件操作),获取结果后再组织自然语言回复。

## 文件结构

```
Practice02/
├── chat.py              # 主脚本
├── config.ini           # 本地配置(含真实密钥,已被 .gitignore 忽略)
├── config.ini.example   # 安全配置模板,可提交
├── architecture.mmd     # 代码架构图(Mermaid 格式)
└── README.md            # 本文档
```

## 运行

```powershell
# 1. 编辑 config.ini,填入真实 API Key(硅基流动 SiliconFlow)
# 2. 运行
cd e:\LLM\Agent\Practice02
python chat.py
```

## 配置项(config.ini)

| 字段 | 说明 | 默认 |
|------|------|------|
| `base_url` | OpenAI 兼容接口地址 | `https://api.siliconflow.cn/v1` |
| `api_key` | API 密钥 | — |
| `model` | 模型名(需支持 Function Calling) | `Qwen/Qwen3.5-4B` |
| `enable_thinking` | 是否输出思考过程 | `false` |
| `typewriter_delay` | 打字机逐字延时(秒) | `0.01` |
| `timeout` | 请求超时(秒) | `60` |

## 架构

完整 Mermaid 架构图见 [architecture.mmd](architecture.mmd)。

核心是 **Function Calling 双请求流程**:

```
用户输入 → 请求1(带 tools)→ LLM 返回 tool_calls
    → 本地 run_tool 执行 → 追加 tool 消息
    → 请求2(不带 tools)→ LLM 基于工具结果组织最终回答
```

## 工具清单

### 日期类(3 个)

| 工具 | 功能 | 实现方式 |
|------|------|----------|
| `getDate` | 获取当前电脑日期和时间 | `subprocess` 调 `cmd /c date /t & time /t`(Windows)或 `date`(Linux/macOS) |
| `isLeapYear` | 判断某年是否闰年 | Python 公历规则:`(year%4==0 and year%100!=0) or year%400==0` |
| `daysBetween` | 两个日期相隔天数(YYYY-MM-DD) | `datetime.strptime` 解析 + 日期对象相减取绝对值 |

### 文件操作类(10 个,全部 Python 标准库实现,跨平台)

| 工具 | 功能 | 核心函数 |
|------|------|----------|
| `getCwd` | 当前工作目录 | `os.getcwd()` |
| `listDir` | 列出目录内容 | `Path.iterdir()` + 排序,目录后加 `/` |
| `getFileType` | 文件/目录类型、扩展名、大小 | `Path.is_dir()` / `suffix` / `stat().st_size` |
| `readFile` | 读取纯文本文件(≤2000 字符) | `Path.read_text`,扩展名白名单 + UTF-8 强制 + 截断 |
| `createFile` | 在指定目录创建新文件 | `dir_p / filename`,已存在则报错 |
| `writeFile` | 覆盖写入纯文本文件 | `Path.write_text`,扩展名白名单校验 |
| `createDir` | 创建目录(含父目录) | `mkdir(parents=True)` |
| `removeDir` | 删除目录及全部内容 | `shutil.rmtree`(支持非空,危险操作) |
| `removeFile` | 删除文件 | `Path.unlink()` |
| `renamePath` | 重命名文件或目录 | `Path.rename()` |

### 纯文本白名单(用于 readFile / writeFile)

`.txt .py .json .md .csv .html .htm .css .js .ini .yml .yaml .log .cfg .xml .rtf .properties .toml`

不在白名单的文件(如 `.exe .jpg .pdf`)会被拒绝读取/写入。

## 主循环流程

```
while True:
    1. input("你: ") → messages.append(user)
    2. POST /chat/completions(带 tools,stream=True)
    3. _stream_collect 解析流式响应
       ├─ 有 tool_calls → run_tool 逐个执行 → 追加 tool 消息 → 二次 POST(不带 tools)
       └─ 无 tool_calls → 直接打字机输出
    4. 追加 assistant 消息
    5. 历史截断(>MAX_HISTORY=20 保留最近 20 条)
    6. 异常捕获:requests.RequestException / Exception → 回滚 user 消息 + 提示后继续
    Ctrl+C → KeyboardInterrupt → 打印退出提示
```

## 对话上下文

- `messages` 列表跨轮持久化,每轮发送完整历史给 API
- `MAX_HISTORY = 20`,超出丢弃最早消息,防止上下文无限增长
- 历史严格 user → assistant → tool → assistant 交替,结构合法

## 错误处理

| 场景 | 行为 |
|------|------|
| 网络超时 / HTTP 错 | 捕获 `RequestException`,回滚刚追加的 user 消息,打印提示,循环继续 |
| 工具执行异常 | 捕获 `Exception`,同上 |
| Ctrl+C | 捕获 `KeyboardInterrupt`,打印"已退出对话。",干净结束 |
| 日期格式错误 | `daysBetween` 返回中文错误提示 |
| 文件类型不支持 | `readFile` / `writeFile` 返回中文错误提示 |
| 路径不存在 / 已存在 | 各工具返回中文错误提示 |

## 触发示例

```
你: 现在几点了?
  → getDate
你: 2024 是闰年吗?
  → isLeapYear(year=2024)
你: 2026-10-19 到 2027-02-14 隔多少天?
  → daysBetween(date1="2026-10-19", date2="2027-02-14")
你: 列出 e:\LLM\Agent 目录下有什么?
  → listDir(path="e:\\LLM\\Agent")
你: 帮我在 Practice02 下创建 test.txt,内容是 Hello
  → createFile(dir="...", filename="test.txt", content="Hello")
你: 当前工作目录在哪里?
  → getCwd
```

## 安全注意

1. **API Key 泄露**:真实密钥已写入 `config.ini`,该文件被 `.gitignore` 忽略,**不会被提交**。但密钥曾在对话中明文出现过,建议定期轮换。
2. **removeDir 危险操作**:递归删除非空目录,无二次确认,对话中触发会真实删除本地文件。工具 description 已标注"危险操作,请谨慎使用",但仍需小心。
3. **writeFile 覆盖写入**:直接覆盖目标文件,无备份机制。
4. **不要用 root / 管理员权限运行**:工具直接操作本地文件系统,用普通权限运行可降低误伤范围。
