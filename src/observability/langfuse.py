from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from functools import lru_cache
from typing import Any, TypeVar

import httpx
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langfuse import Langfuse
from loguru import logger

from src.config import settings

T = TypeVar("T")


def is_langfuse_enabled() -> bool:
    return bool(
        settings.langfuse_enabled
        and settings.langfuse_public_key
        and settings.langfuse_secret_key
        and settings.langfuse_base_url
    )


@lru_cache(maxsize=1)
def get_langfuse_client() -> Langfuse | None:
    if not is_langfuse_enabled():
        return None

    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url,
        environment=settings.langfuse_environment,
        httpx_client=httpx.Client(trust_env=False),
    )


def start_observation(**kwargs) -> Any:
    client = get_langfuse_client()
    if client is None:
        return nullcontext(None)
    return client.start_as_current_observation(**kwargs)


def flush_langfuse() -> None:
    client = get_langfuse_client()
    if client is not None:
        client.flush()


def shutdown_langfuse() -> None:
    client = get_langfuse_client()
    if client is not None:
        client.shutdown()


def safe_observation_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")

    if isinstance(value, BaseMessage):
        return serialize_langchain_message(value)

    if isinstance(value, dict):
        return {str(k): safe_observation_value(v) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [safe_observation_value(v) for v in value]

    if hasattr(value, "model_dump"):
        try:
            return safe_observation_value(value.model_dump(mode="json"))
        except Exception:
            logger.debug("safe_observation_value: model_dump 序列化失败", exc_info=True)

    if hasattr(value, "dict"):
        try:
            return safe_observation_value(value.dict())
        except Exception:
            logger.debug("safe_observation_value: dict 序列化失败", exc_info=True)

    return str(value)


def serialize_langchain_message(message: BaseMessage) -> dict[str, Any]:
    data: dict[str, Any] = {
        "message_type": message.__class__.__name__,
        "role": getattr(message, "type", message.__class__.__name__.lower()),
        "content": safe_observation_value(getattr(message, "content", None)),
    }

    if getattr(message, "name", None):
        data["name"] = message.name

    additional_kwargs = getattr(message, "additional_kwargs", None)
    if additional_kwargs:
        data["additional_kwargs"] = safe_observation_value(additional_kwargs)

    if isinstance(message, AIMessage):
        if message.tool_calls:
            data["tool_calls"] = safe_observation_value(message.tool_calls)
        if getattr(message, "usage_metadata", None):
            data["usage_metadata"] = safe_observation_value(message.usage_metadata)
        if getattr(message, "response_metadata", None):
            data["response_metadata"] = safe_observation_value(message.response_metadata)

    if isinstance(message, ToolMessage):
        data["tool_call_id"] = message.tool_call_id

    return data


def serialize_langchain_messages(messages: list[Any]) -> list[Any]:
    return [
        serialize_langchain_message(message) if isinstance(message, BaseMessage) else safe_observation_value(message)
        for message in messages
    ]


def extract_langchain_usage(response: AIMessage) -> dict[str, int] | None:
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict) and usage:
        return {str(k): int(v) for k, v in usage.items() if isinstance(v, int | float)}

    response_metadata = getattr(response, "response_metadata", None) or {}
    for key in ("token_usage", "usage"):
        raw_usage = response_metadata.get(key)
        if isinstance(raw_usage, dict) and raw_usage:
            normalized: dict[str, int] = {}
            for k, v in raw_usage.items():
                if isinstance(v, int | float):
                    normalized[str(k)] = int(v)
            if normalized:
                return normalized

    return None


def get_langchain_model_name(llm: Any) -> str | None:
    for attr in ("model_name", "model"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


async def observe_langchain_generation(
    *,
    name: str,
    llm: Any,
    messages: list[Any],
    invoke: Callable[[], Awaitable[AIMessage]],
    metadata: dict[str, Any] | None = None,
) -> AIMessage:
    with start_observation(
        name=name,
        as_type="generation",
        input=serialize_langchain_messages(messages),
        metadata=safe_observation_value(metadata or {}),
        model=get_langchain_model_name(llm),
    ) as generation:
        try:
            response = await invoke()
        except Exception as exc:
            if generation is not None:
                generation.update(
                    level="ERROR",
                    status_message=str(exc),
                    output={"error": str(exc)},
                )
            raise

        if generation is not None:
            update_payload: dict[str, Any] = {
                "output": serialize_langchain_message(response),
            }
            usage = extract_langchain_usage(response)
            if usage:
                update_payload["usage_details"] = usage
            generation.update(**update_payload)

        return response


def wrap_tool_coroutine(
    *,
    name: str,
    category: str,
    coroutine: Callable[..., Awaitable[T]],
) -> Callable[..., Awaitable[T]]:
    async def traced_coroutine(**kwargs) -> T:
        with start_observation(
            name=name,
            as_type="tool",
            input=safe_observation_value(kwargs),
            metadata={"tool_category": category},
        ) as tool_obs:
            try:
                result = await coroutine(**kwargs)
            except Exception as exc:
                if tool_obs is not None:
                    tool_obs.update(
                        level="ERROR",
                        status_message=str(exc),
                        output={"error": str(exc)},
                    )
                raise

            if tool_obs is not None:
                tool_obs.update(output=safe_observation_value(result))
            return result

    return traced_coroutine
