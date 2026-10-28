"""OpenAI 兼容接口的流式客户端,只负责网络通信与响应解析。"""

import json
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

from .config import LLMConfig


@dataclass
class ToolCall:
    """流式累积中的一个工具调用。"""

    id: str | None = None
    name: str | None = None
    arguments: str = ""


@dataclass(frozen=True)
class StreamDelta:
    """文本增量片段。"""

    text: str


@dataclass(frozen=True)
class LLMResponse:
    """一次流式响应聚合后的完整结果。"""

    content: str
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)


# 流式事件:文本片段,或流结束时的完整响应
StreamEvent = StreamDelta | LLMResponse


class LLMClient:
    """封装 chat/completions 流式调用。"""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[StreamEvent]:
        """发起流式请求:先逐段产出 StreamDelta,最后产出 LLMResponse。

        tools 为 None 时不带工具定义(用于工具执行后的最终回答请求)。
        """
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "stream": True,
            "enable_thinking": self._config.enable_thinking,
        }
        if tools:
            payload["tools"] = tools

        resp = requests.post(
            f"{self._config.base_url}/chat/completions",
            headers=self._config.headers,
            json=payload,
            timeout=self._config.timeout,
            stream=True,
        )
        resp.raise_for_status()

        content_parts: list[str] = []
        tool_calls: dict[int, ToolCall] = {}

        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8")
            if line.startswith("data: "):
                line = line[6:]
            if line == "[DONE]":
                break

            chunk = json.loads(line)
            choices = chunk.get("choices")
            if not choices:
                # 服务端会在 HTTP 200 的流中夹带错误事件(如限流),缺 choices 字段;
                # 抛出带服务端信息的异常,而不是裸 KeyError
                detail = chunk.get("message") or chunk.get("error") or chunk
                raise RuntimeError(f"LLM 流式响应返回错误: {detail}")
            delta = choices[0]["delta"]

            text = delta.get("content")
            if text:
                content_parts.append(text)
                yield StreamDelta(text)

            for item in delta.get("tool_calls") or []:
                index = item["index"]
                call = tool_calls.setdefault(index, ToolCall())
                if item.get("id"):
                    call.id = item["id"]
                function = item.get("function") or {}
                if function.get("name"):
                    call.name = function["name"]
                if function.get("arguments"):
                    call.arguments += function["arguments"]

        yield LLMResponse(
            content="".join(content_parts),
            tool_calls=tuple(tool_calls[index] for index in sorted(tool_calls)),
        )
