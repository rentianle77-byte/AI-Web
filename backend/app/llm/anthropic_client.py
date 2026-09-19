"""Claude 官方 SDK 适配(Anthropic Messages API)。"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import anthropic

from ..config import settings
from .base import LLMClient, LLMEvent, ToolCall, ToolSpec

# 这些模型不接受 thinking.budget_tokens,用 output_config.effort 控制思考深度
_EFFORT_MODELS = ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-5", "claude-fable-5")


def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """把统一格式翻译成 Anthropic 的 content block 结构。"""
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc.get("arguments") or {}})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if m.get("is_error"):
                block["is_error"] = True
            # 连续的 tool_result 合并进同一条 user 消息(API 要求)
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self) -> None:
        kwargs: dict[str, Any] = {"api_key": settings.anthropic_api_key}
        if settings.anthropic_base_url:
            kwargs["base_url"] = settings.anthropic_base_url
        self._client = anthropic.AsyncAnthropic(**kwargs)
        self.model = settings.anthropic_model

    def _extra(self) -> dict:
        extra: dict[str, Any] = {}
        if any(self.model.startswith(m) for m in _EFFORT_MODELS):
            extra["thinking"] = {"type": "adaptive"}
            extra["output_config"] = {"effort": settings.llm_effort}
        return extra

    async def astream(self, system: str, messages: list[dict], tools: list[ToolSpec] | None = None) -> AsyncIterator[LLMEvent]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": settings.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": _to_anthropic_messages(messages),
            **self._extra(),
        }
        if tools:
            payload["tools"] = [t.to_anthropic() for t in tools]
        try:
            async with self._client.messages.stream(**payload) as stream:
                async for event in stream:
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield LLMEvent(type="text_delta", text=event.delta.text)
                final = await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            yield LLMEvent(type="error", text=f"Claude 接口出错({exc.status_code}):{getattr(exc, 'message', str(exc))}")
            return
        except anthropic.APIConnectionError as exc:
            yield LLMEvent(type="error", text=f"连不上 Claude 接口:{exc}")
            return

        for block in final.content:
            if block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else json.loads(block.input or "{}")
                yield LLMEvent(type="tool_call", tool_call=ToolCall(id=block.id, name=block.name, arguments=args))
        usage = {"input_tokens": final.usage.input_tokens, "output_tokens": final.usage.output_tokens}
        yield LLMEvent(type="done", stop_reason=final.stop_reason or "end_turn", usage=usage)
