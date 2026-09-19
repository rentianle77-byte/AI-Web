"""OpenAI 兼容接口适配(DeepSeek / 通义千问 / Kimi / 智谱 / OpenAI 都能用)。"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from openai import AsyncOpenAI, APIConnectionError, APIStatusError

from ..config import settings
from .base import LLMClient, LLMEvent, ToolCall, ToolSpec


def _to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
            calls = m.get("tool_calls") or []
            if calls:
                msg["tool_calls"] = [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments") or {}, ensure_ascii=False)}}
                    for c in calls
                ]
            out.append(msg)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
    return out


class OpenAICompatClient(LLMClient):
    provider = "openai"

    def __init__(self) -> None:
        self._client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url, timeout=120.0)
        self.model = settings.openai_model

    async def astream(self, system: str, messages: list[dict], tools: list[ToolSpec] | None = None) -> AsyncIterator[LLMEvent]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _to_openai_messages(system, messages),
            "stream": True,
            "max_tokens": settings.max_tokens,
        }
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
            payload["tool_choice"] = "auto"

        # 流式增量里 tool_calls 是按 index 拼接的
        acc: dict[int, dict[str, Any]] = {}
        stop_reason = "end_turn"
        usage: dict[str, int] = {}
        try:
            stream = await self._client.chat.completions.create(**payload)
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = {"input_tokens": chunk.usage.prompt_tokens or 0, "output_tokens": chunk.usage.completion_tokens or 0}
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if getattr(delta, "content", None):
                    yield LLMEvent(type="text_delta", text=delta.content)
                for tc in getattr(delta, "tool_calls", None) or []:
                    slot = acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments
                if choice.finish_reason:
                    stop_reason = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}.get(choice.finish_reason, choice.finish_reason)
        except APIStatusError as exc:
            yield LLMEvent(type="error", text=f"模型接口出错({exc.status_code}):{exc.message}")
            return
        except APIConnectionError as exc:
            yield LLMEvent(type="error", text=f"连不上模型接口:{exc}")
            return

        for idx in sorted(acc):
            slot = acc[idx]
            if not slot["name"]:
                continue
            try:
                args = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                args = {"__raw__": slot["args"]}
            yield LLMEvent(type="tool_call", tool_call=ToolCall(id=slot["id"] or f"call_{idx}", name=slot["name"], arguments=args if isinstance(args, dict) else {}))
        if acc and stop_reason == "end_turn":
            stop_reason = "tool_use"
        yield LLMEvent(type="done", stop_reason=stop_reason, usage=usage)
