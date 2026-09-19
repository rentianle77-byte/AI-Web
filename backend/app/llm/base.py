"""统一的 LLM 抽象:不同厂商的差异在这里抹平。

对上层(Agent 运行时)只暴露一个方法:
    astream(system, messages, tools) -> AsyncIterator[LLMEvent]

事件流:
    text_delta   增量文本
    tool_call    模型要调工具(完整参数)
    done         本轮结束,带 stop_reason
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMEvent:
    type: Literal["text_delta", "tool_call", "done", "error"]
    text: str = ""
    tool_call: ToolCall | None = None
    stop_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class ToolSpec:
    """与厂商无关的工具定义。"""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_anthropic(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}

    def to_openai(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


class LLMClient:
    provider: str = "base"
    model: str = ""

    async def astream(
        self,
        system: str,
        messages: list[dict],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def acomplete(self, system: str, messages: list[dict], tools: list[ToolSpec] | None = None) -> tuple[str, list[ToolCall], str]:
        """非流式便捷封装:返回 (文本, 工具调用列表, stop_reason)。"""
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        stop = "end_turn"
        async for ev in self.astream(system, messages, tools):
            if ev.type == "text_delta":
                text_parts.append(ev.text)
            elif ev.type == "tool_call" and ev.tool_call:
                calls.append(ev.tool_call)
            elif ev.type == "done":
                stop = ev.stop_reason or stop
            elif ev.type == "error":
                raise RuntimeError(ev.text)
        return "".join(text_parts), calls, stop


# ---------------- 统一的会话历史结构 ----------------
# messages 里每条是:
#   {"role": "user", "content": "..."}
#   {"role": "assistant", "content": "...", "tool_calls": [ToolCall-like dict]}
#   {"role": "tool", "tool_call_id": "...", "name": "...", "content": "..."}
# 各厂商适配器负责翻译成自己的格式。
