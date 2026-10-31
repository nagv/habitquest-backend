"""The chat model: any OpenAI-compatible endpoint (Nebius Token Factory) through LangChain."""

import httpx
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from habitquest_backend.config import Settings

# Reasoning models spend part of this budget on hidden reasoning, so it must leave room for the answer.
MAX_TOKENS = 4000


def http_client(settings: Settings) -> httpx.Client:
    return httpx.Client(timeout=settings.ai_timeout_seconds)


def chat_model(settings: Settings, *, small: bool = False, temperature: float = 0) -> BaseChatModel:
    """The large model, or with `small` the faster one for buddy lines and safety checks."""
    return init_chat_model(
        model=(settings.openai_small_model if small else "") or settings.openai_model,
        model_provider="openai",
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key.get_secret_value() if settings.openai_api_key else "",
        temperature=temperature,
        max_tokens=MAX_TOKENS,
        max_retries=1,
        http_client=http_client(settings),
    )
