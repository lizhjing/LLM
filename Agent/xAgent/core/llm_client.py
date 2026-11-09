"""OpenAI 兼容接口的流式客户端,只负责网络通信与响应解析。"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

from .config import LLMConfig

logger = logging.getLogger(__name__)


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
    # 请求侧的 prompt token 统计(含 system、历史与工具消息),来自 API usage;
    # 服务端未提供时为 None,由调用方自行估算
    prompt_tokens: int | None = None


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
        连接超时固定 10 秒,读超时取配置值——requests 的读超时约束的是相邻
        字节间隔,因此实际约束的是高峰期排队与首 token 延迟。
        连接/读超时且尚未产出任何增量时自动重试一次;已开始输出后则直接抛出,
        避免消费者重复渲染。HTTP 状态错误与流内业务错误不重试。
        """
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "stream": True,
            "enable_thinking": self._config.enable_thinking,
            # OpenAI 兼容约定:请求在流末尾附带 usage 统计块,
            # 供上下文占用监控使用;不支持的服务端一般会忽略该字段
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools

        # 连接超时来自配置;读超时约束相邻字节间隔,实际约束的是高峰期排队与首 token 延迟
        timeout = (self._config.connect_timeout, self._config.timeout)
        for attempt in (1, 2):
            received = False
            try:
                for event in self._stream_once(payload, timeout):
                    if isinstance(event, StreamDelta):
                        received = True
                    yield event
                return
            except requests.RequestException as exc:
                # 覆盖首包前的连接失败/读超时/传输中断(ChunkedEncodingError 等);
                # 已收到增量后不重试,避免重复输出
                logger.warning("LLM 请求异常(第 %d/3 次尝试): %s", attempt + 1, exc)
                if received or attempt == 2:
                    raise

    def _stream_once(
        self,
        payload: dict[str, Any],
        timeout: tuple[float, float],
    ) -> Iterator[StreamEvent]:
        """执行一次流式请求并解析 SSE 增量(不含重试逻辑)。"""
        resp = requests.post(
            f"{self._config.base_url}/chat/completions",
            headers=self._config.headers,
            json=payload,
            timeout=timeout,
            stream=True,
        )
        resp.raise_for_status()

        content_parts: list[str] = []
        tool_calls: dict[int, ToolCall] = {}
        prompt_tokens: int | None = None

        try:
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8")
                if line.startswith("data: "):
                    line = line[6:]
                if line == "[DONE]":
                    break

                chunk = json.loads(line)

                # usage 统计:部分服务端每个 chunk 都携带(反复覆盖即取总量),
                # include_usage 则在流末尾以独立 chunk 提供
                usage = chunk.get("usage")
                if isinstance(usage, dict) and usage.get("prompt_tokens") is not None:
                    prompt_tokens = usage["prompt_tokens"]

                choices = chunk.get("choices")
                if not choices:
                    if usage:
                        break  # include_usage 的收尾统计块:无增量内容,流结束
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
        finally:
            # 确保连接归还连接池(含 [DONE] 提前 break 与生成器提前关闭的情况)
            resp.close()

        yield LLMResponse(
            content="".join(content_parts),
            tool_calls=tuple(tool_calls[index] for index in sorted(tool_calls)),
            prompt_tokens=prompt_tokens,
        )
