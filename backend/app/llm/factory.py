"""按配置选模型。缺 key 自动降级到演示模型,永远不会因为没 key 起不来。"""
from __future__ import annotations

import logging

from ..config import settings
from .base import LLMClient

log = logging.getLogger(__name__)
_client: LLMClient | None = None


def get_client() -> LLMClient:
    global _client
    if _client is not None:
        return _client
    provider = settings.llm_provider
    if provider == "anthropic" and settings.anthropic_api_key:
        from .anthropic_client import AnthropicClient

        _client = AnthropicClient()
    elif provider == "openai" and settings.openai_api_key:
        from .openai_client import OpenAICompatClient

        _client = OpenAICompatClient()
    else:
        if provider not in ("mock", ""):
            log.warning("LLM_PROVIDER=%s 但没有对应的 API Key,降级为演示模型", provider)
        from .mock import MockClient

        _client = MockClient()
    log.info("LLM provider=%s model=%s", _client.provider, _client.model)
    return _client


def reset_client() -> None:
    global _client
    _client = None
