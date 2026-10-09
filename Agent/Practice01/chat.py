import configparser
import json
import time
from pathlib import Path

import requests

# 读取同目录下的 config.ini
cfg = configparser.ConfigParser()
cfg.read(Path(__file__).parent / "config.ini", encoding="utf-8")
base_url = cfg["llm"]["base_url"]
api_key = cfg["llm"]["api_key"]
model = cfg["llm"]["model"]
# 思考型模型(如 Qwen3 / Qwen3.5 系列)默认会先输出一段思考过程,
# 那段时间里 delta 里只有 reasoning_content、没有 content,界面看起来像卡死,
# 关掉思考可直接拿到正文,首字延迟从数十秒降到 1 秒内。
enable_thinking = cfg.getboolean("llm", "enable_thinking", fallback=False)
typewriter_delay = cfg.getfloat("llm", "typewriter_delay", fallback=0.01)
timeout = cfg.getfloat("llm", "timeout", fallback=60)

# 多轮对话上下文历史
messages = []
MAX_HISTORY = 20  # 最多保留最近的消息条数,超出则丢弃最早的,防止上下文无限增长

# 持续对话,直到用户按下 Ctrl+C 中断
try:
    while True:
        # 发送一条用户输入并接收一次回复
        user_input = input("你: ")
        messages.append({"role": "user", "content": user_input})
        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "enable_thinking": enable_thinking,
                },
                timeout=timeout,
                stream=True,
            )
            resp.raise_for_status()

            print("AI: ", end="", flush=True)
            reply = ""
            for line in resp.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8")
                if line.startswith("data: "):
                    line = line[6:]
                if line == "[DONE]":
                    break
                chunk = json.loads(line)
                content = chunk["choices"][0]["delta"].get("content")
                if not content:
                    continue
                reply += content
                print(content, end="", flush=True)
                time.sleep(typewriter_delay)
            print()
            # 把本轮 AI 回复加入历史,供下一轮作为上下文
            messages.append({"role": "assistant", "content": reply})
            # 边界处理:历史过长则只保留最近 MAX_HISTORY 条
            if len(messages) > MAX_HISTORY:
                messages = messages[-MAX_HISTORY:]
        except requests.RequestException as e:
            # 请求失败:回滚刚加入的用户消息,保持历史 user/assistant 交替
            if messages and messages[-1]["role"] == "user":
                messages.pop()
            print(f"\n[请求失败,本轮已跳过:{e}]")
        except Exception as e:
            # 其他异常(解析等):同样回滚,避免整个会话崩溃
            if messages and messages[-1]["role"] == "user":
                messages.pop()
            print(f"\n[本轮出错,已跳过:{e}]")
except KeyboardInterrupt:
    print("\n已退出对话。")
