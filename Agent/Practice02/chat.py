import configparser
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# 纯文本扩展名白名单,用于 readFile / writeFile 判断(模块级,避免每次调用重新构建)
_TEXT_EXTS = {
    ".txt", ".py", ".json", ".md", ".csv", ".html", ".htm",
    ".css", ".js", ".ini", ".yml", ".yaml", ".log", ".cfg",
    ".xml", ".rtf", ".properties", ".toml",
}

# ===== Function Call 工具定义 =====
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "getDate",
            "description": "获取当前电脑的日期和时间",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "isLeapYear",
            "description": "判断某个年份是否是闰年(公历)。规则:四年一闰,百年不闰,四百年再闰。",
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {
                        "type": "integer",
                        "description": "要判断的年份,如 2024"
                    }
                },
                "required": ["year"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "daysBetween",
            "description": "计算两个日期之间相隔的自然天数。日期格式必须为 YYYY-MM-DD,如 2026-10-19。返回值为天数绝对值,不区分先后顺序。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date1": {
                        "type": "string",
                        "description": "第一个日期,格式 YYYY-MM-DD,如 2026-10-19"
                    },
                    "date2": {
                        "type": "string",
                        "description": "第二个日期,格式 YYYY-MM-DD,如 2026-12-25"
                    }
                },
                "required": ["date1", "date2"]
            }
        }
    },
    # ===== 文件操作工具(全部用 Python 标准库实现,跨平台一致) =====
    {
        "type": "function",
        "function": {
            "name": "getCwd",
            "description": "获取当前工作目录(脚本运行时的工作路径)",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "listDir",
            "description": "列出指定目录下的所有文件和子目录名称。目录名后加 / 以示区分。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要列出的目录路径,如 e:\\LLM 或 ."
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "getFileType",
            "description": "获取指定路径的类型(文件/目录/不存在),文件额外返回扩展名和大小。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件或目录的路径"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "readFile",
            "description": "读取指定纯文本文件的内容。仅支持 txt/py/json/md/csv/html/css/js/ini/yml/yaml/log/cfg/xml 等纯文本格式,不支持二进制文件(如 exe、jpg、pdf)。内容超过2000字符会截断。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要读取的文件路径"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "createFile",
            "description": "在指定目录下创建一个新的纯文本文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "dir": {
                        "type": "string",
                        "description": "目标目录路径,如 e:\\LLM\\Agent"
                    },
                    "filename": {
                        "type": "string",
                        "description": "要创建的文件名,如 hello.txt"
                    },
                    "content": {
                        "type": "string",
                        "description": "文件初始内容,可为空字符串"
                    }
                },
                "required": ["dir", "filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "writeFile",
            "description": "覆盖写入指定纯文本文件的内容。仅支持纯文本格式文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要编辑的文件路径"
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的新内容,将覆盖原文件"
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "createDir",
            "description": "创建一个新目录,若父目录不存在会一并创建(等价于 mkdir -p)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要创建的目录路径"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "removeDir",
            "description": "删除指定目录及其下所有内容(包括子目录和文件)。危险操作,请谨慎使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要删除的目录路径"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "removeFile",
            "description": "删除指定文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要删除的文件路径"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "renamePath",
            "description": "重命名文件或目录(仅改名称,不能跨盘符移动)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_path": {
                        "type": "string",
                        "description": "原路径(文件或目录)"
                    },
                    "new_path": {
                        "type": "string",
                        "description": "新路径(仅改名称,不能跨盘符)"
                    }
                },
                "required": ["old_path", "new_path"]
            }
        }
    }
]


def run_tool(name, arguments):
    """执行工具调用,返回结果字符串。
    用系统自带的 date 命令而非 Python datetime,跨平台分支。"""
    if name == "getDate":
        if sys.platform == "win32":
            # Windows: cmd 内置 date /t 和 time /t,不交互、不提示
            result = subprocess.run(
                ["cmd", "/c", "date /t & time /t"],
                capture_output=True, text=True, timeout=5,
            )
        else:
            # Linux / macOS
            result = subprocess.run(
                ["date"], capture_output=True, text=True, timeout=5,
            )
        return result.stdout.strip()
    if name == "isLeapYear":
        year = arguments["year"]
        if (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0):
            return f"{year} 年是闰年"
        return f"{year} 年不是闰年"
    if name == "daysBetween":
        # 输入校验:必须为 YYYY-MM-DD 格式,且日期真实存在(如 2026-02-30 非法)
        try:
            d1 = datetime.strptime(arguments["date1"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            return "格式错误:date1 必须为 YYYY-MM-DD,且是真实存在的日期"
        try:
            d2 = datetime.strptime(arguments["date2"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            return "格式错误:date2 必须为 YYYY-MM-DD,且是真实存在的日期"
        diff = abs((d2 - d1).days)
        return f"{d1} 和 {d2} 之间相隔 {diff} 天"

    # ===== 文件操作工具实现(全部用 Python 标准库,跨平台一致) =====

    if name == "getCwd":
        return os.getcwd()

    if name == "listDir":
        path = arguments["path"]
        p = Path(path)
        if not p.exists():
            return f"路径不存在: {path}"
        if not p.is_dir():
            return f"不是目录: {path}"
        items = sorted(p.iterdir())
        lines = [i.name + ("/" if i.is_dir() else "") for i in items]
        return "\n".join(lines) if lines else "(空目录)"

    if name == "getFileType":
        path = arguments["path"]
        p = Path(path)
        if not p.exists():
            return f"路径不存在: {path}"
        if p.is_dir():
            return f"目录(文件夹): {p}"
        ext = p.suffix.lower() or "(无扩展名)"
        size = p.stat().st_size
        return f"文件: {p.name}  扩展名: {ext}  大小: {size} 字节"

    if name == "readFile":
        path = arguments["path"]
        p = Path(path)
        if not p.exists():
            return f"文件不存在: {path}"
        if p.is_dir():
            return f"路径是目录而非文件: {path}"
        if p.suffix.lower() not in _TEXT_EXTS:
            return f"不支持读取该文件类型({p.suffix}),仅支持纯文本文件"
        try:
            content = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"文件编码非 UTF-8,无法以纯文本方式读取"
        if len(content) > 2000:
            content = content[:2000] + "\n...(内容已截断,超过2000字符)"
        return content

    if name == "createFile":
        dir_path = arguments["dir"]
        filename = arguments["filename"]
        content = arguments.get("content", "")
        dir_p = Path(dir_path)
        if not dir_p.exists():
            return f"目录不存在: {dir_path}"
        if not dir_p.is_dir():
            return f"路径不是目录: {dir_path}"
        file_path = dir_p / filename
        if file_path.exists():
            return f"文件已存在: {file_path}"
        file_path.write_text(content, encoding="utf-8")
        return f"已创建文件: {file_path}"

    if name == "writeFile":
        path = arguments["path"]
        content = arguments["content"]
        p = Path(path)
        if not p.exists():
            return f"文件不存在: {path}"
        if p.is_dir():
            return f"路径是目录而非文件: {path}"
        if p.suffix.lower() not in _TEXT_EXTS:
            return f"不支持编辑该文件类型({p.suffix}),仅支持纯文本文件"
        p.write_text(content, encoding="utf-8")
        return f"已写入文件: {path}({len(content)} 字符)"

    if name == "createDir":
        path = arguments["path"]
        p = Path(path)
        if p.exists():
            return f"路径已存在: {path}"
        p.mkdir(parents=True, exist_ok=True)
        return f"已创建目录: {path}"

    if name == "removeDir":
        path = arguments["path"]
        p = Path(path)
        if not p.exists():
            return f"目录不存在: {path}"
        if not p.is_dir():
            return f"路径不是目录: {path}"
        shutil.rmtree(p)
        return f"已删除目录及其全部内容: {path}"

    if name == "removeFile":
        path = arguments["path"]
        p = Path(path)
        if not p.exists():
            return f"文件不存在: {path}"
        if p.is_dir():
            return f"路径是目录而非文件,请用 removeDir: {path}"
        p.unlink()
        return f"已删除文件: {path}"

    if name == "renamePath":
        old = arguments["old_path"]
        new = arguments["new_path"]
        old_p = Path(old)
        new_p = Path(new)
        if not old_p.exists():
            return f"原路径不存在: {old}"
        if new_p.exists():
            return f"新路径已存在: {new}"
        old_p.rename(new_p)
        return f"已重命名: {old} → {new}"

    return None


def _print_typing(text, delay):
    """打字机效果逐字打印"""
    for ch in text:
        print(ch, end="", flush=True)
        time.sleep(delay)


def _stream_collect(resp):
    """遍历流式响应,同时收集 content 和 tool_calls。
    返回 (content_str, tool_calls_dict)
    tool_calls_dict: {index(int): {"id": ..., "name": ..., "arguments": "..."}}"""
    content_parts = []
    tool_calls = {}

    for line in resp.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8")
        if line.startswith("data: "):
            line = line[6:]
        if line == "[DONE]":
            break
        chunk = json.loads(line)
        delta = chunk["choices"][0]["delta"]

        c = delta.get("content")
        if c:
            content_parts.append(c)

        for tc in delta.get("tool_calls") or []:
            idx = tc["index"]
            if idx not in tool_calls:
                tool_calls[idx] = {"id": None, "name": None, "arguments": ""}
            if tc.get("id"):
                tool_calls[idx]["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                tool_calls[idx]["name"] = fn["name"]
            if fn.get("arguments"):
                tool_calls[idx]["arguments"] += fn["arguments"]

    return "".join(content_parts), tool_calls


# ===== 读取配置 =====
cfg = configparser.ConfigParser()
cfg.read(Path(__file__).parent / "config.ini", encoding="utf-8")
base_url = cfg["llm"]["base_url"]
api_key = cfg["llm"]["api_key"]
model = cfg["llm"]["model"]
enable_thinking = cfg.getboolean("llm", "enable_thinking", fallback=False)
typewriter_delay = cfg.getfloat("llm", "typewriter_delay", fallback=0.01)
timeout = cfg.getfloat("llm", "timeout", fallback=60)

HEADERS = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

# ===== 多轮对话上下文 =====
messages = []
MAX_HISTORY = 20

# ===== 主循环 =====
try:
    while True:
        user_input = input("你: ")
        messages.append({"role": "user", "content": user_input})
        try:
            # 第一次请求:带 tools,流式
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers=HEADERS,
                json={
                    "model": model,
                    "messages": messages,
                    "tools": TOOLS,
                    "stream": True,
                    "enable_thinking": enable_thinking,
                },
                timeout=timeout,
                stream=True,
            )
            resp.raise_for_status()

            content, tool_calls = _stream_collect(resp)

            if tool_calls:
                # 模型要求调用工具
                if content:
                    # 模型先输出了部分内容再调工具(少见但兼容)
                    print("AI: ", end="", flush=True)
                    _print_typing(content, typewriter_delay)
                    print()
                first_tool = next(iter(tool_calls.values()))
                print(f"[调用工具: {first_tool['name']}]")

                # 构造 assistant 消息(含完整 tool_calls)追加到历史
                messages.append({
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"] or "{}",
                            },
                        }
                        for tc in tool_calls.values()
                    ],
                })

                # 执行每个工具,追加 tool 消息
                for tc in tool_calls.values():
                    args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    result = run_tool(tc["name"], args)
                    print(f"[工具结果: {result}]")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(result),
                    })

                # 第二次请求:让模型基于工具结果生成最终回答(流式,不再带 tools)
                resp2 = requests.post(
                    f"{base_url}/chat/completions",
                    headers=HEADERS,
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": True,
                        "enable_thinking": enable_thinking,
                    },
                    timeout=timeout,
                    stream=True,
                )
                resp2.raise_for_status()

                print("AI: ", end="", flush=True)
                final_content, _ = _stream_collect(resp2)
                _print_typing(final_content, typewriter_delay)
                print()

                messages.append({"role": "assistant", "content": final_content})
            else:
                # 直接有 content,打字机打印
                print("AI: ", end="", flush=True)
                _print_typing(content, typewriter_delay)
                print()

                messages.append({"role": "assistant", "content": content})

            # 历史截断
            if len(messages) > MAX_HISTORY:
                messages = messages[-MAX_HISTORY:]

        except requests.RequestException as e:
            if messages and messages[-1]["role"] == "user":
                messages.pop()
            print(f"\n[请求失败,本轮已跳过:{e}]")
        except Exception as e:
            if messages and messages[-1]["role"] == "user":
                messages.pop()
            print(f"\n[本轮出错,已跳过:{e}]")
except KeyboardInterrupt:
    print("\n已退出对话。")
