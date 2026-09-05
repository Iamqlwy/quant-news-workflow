"""LLMProvider —— 统一的 LLM 访问入口，封装 provider/model/credentials + 速率限制

对外只暴露行为接口（create_chat_model / chat / chat_json），
不暴露 provider、model、api_key、base_url 等配置细节。
"""

from __future__ import annotations

import asyncio
import random
from functools import lru_cache
from typing import Any

import httpx
import openai
from langchain_core.language_models import BaseChatModel
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from langfuse.openai import AsyncOpenAI as LangfuseAsyncOpenAI
from loguru import logger
from openai import AsyncOpenAI as OpenAIAsyncOpenAI

from src.config import settings
from src.llm.json_utils import _extract_json  # noqa: F401  (re-export for backward compatibility)
from src.observability import get_langfuse_client, is_langfuse_enabled

# ═══════════════════════════════════════════════════
# 速率限制器（模块级单例）
# ═══════════════════════════════════════════════════

# DeepSeek 官方并发限制（按账号，与 API Key 无关）
# https://api-docs.deepseek.com/zh-cn/quick_start/rate_limit
# 注意：本地连接池实际承载能力远低于官方限制，这里使用保守值避免连接池过载
_DS_CONCURRENCY: dict[str, int] = {
    "deepseek-v4-flash": 500,
    "deepseek-v4-pro": 450,
}

# Qwen 并发限制
_QWEN_CONCURRENCY = settings.max_concurrent_llm_calls


class _RateLimiter:
    """按 provider/model 做并发信号量控制，与官方"并发限制"定义一致：
    一个请求从发出到响应完成记为一个并发。
    """

    def __init__(self) -> None:
        self._sems: dict[str, asyncio.Semaphore] = {}

    def _sem(self, provider: str, model: str) -> asyncio.Semaphore | None:
        key = f"{provider}:{model}"
        if key in self._sems:
            return self._sems[key]
        if provider == "qwen":
            limit = _QWEN_CONCURRENCY
        else:
            limit = _DS_CONCURRENCY.get(model, 0)
            if limit <= 0:
                logger.warning(
                    "模型 {} 未在 _DS_CONCURRENCY 中注册，使用保守默认并发限制 50",
                    model,
                )
                limit = 50
        sem = asyncio.Semaphore(limit)
        self._sems[key] = sem
        return sem

    async def acquire(self, provider: str, model: str) -> None:
        sem = self._sem(provider, model)
        if sem is not None:
            await sem.acquire()

    def release(self, provider: str, model: str) -> None:
        sem = self._sem(provider, model)
        if sem is not None:
            sem.release()


_rate_limiter = _RateLimiter()


# ═══════════════════════════════════════════════════
# 共享 httpx 客户端 —— 所有 LLM 调用共用一个连接池
# ═══════════════════════════════════════════════════

_LLM_POOL_KEEPALIVE = 500
_LLM_POOL_MAX_CONNECTIONS = 2000
_LLM_POOL_KEEPALIVE_EXPIRY = 10.0  # 必须 < 服务端/负载均衡 idle timeout（~15s），否则僵尸连接导致 APITimeoutError


@lru_cache(maxsize=1)
def _shared_httpx_client() -> httpx.AsyncClient:
    """模块级共享的异步 httpx 客户端，避免每个 LLMProvider 各自创建连接池。

    默认 httpx keepalive=20 → 高并发下连接池耗尽，实测卡在 ~30 并发。
    """
    limits = httpx.Limits(
        max_connections=_LLM_POOL_MAX_CONNECTIONS,
        max_keepalive_connections=_LLM_POOL_KEEPALIVE,
        keepalive_expiry=_LLM_POOL_KEEPALIVE_EXPIRY,
    )
    return httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=30.0))


# ═══════════════════════════════════════════════════
# 瞬态错误判断（与 agents/base.py 的 _is_transient_error 保持一致）
# ═══════════════════════════════════════════════════

_TRANSIENT_OPENAI = (
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
)


def _is_transient_error(exc: Exception) -> bool:
    """判断异常是否属于瞬态错误（网络故障、超时、服务端 5xx/429），应触发重试。"""
    if isinstance(exc, (asyncio.TimeoutError, ConnectionRefusedError, ConnectionResetError, TimeoutError)):
        return True
    return bool(isinstance(exc, _TRANSIENT_OPENAI))


# ═══════════════════════════════════════════════════
# LLMProvider
# ═══════════════════════════════════════════════════


class LLMProvider:
    """统一的 LLM 访问入口。

    封装 provider / model / api_key / base_url + 速率限制。
    对外只暴露三个行为方法，不暴露任何配置细节。
    """

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        base_url: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._extra_body = extra_body
        self._chat_model: BaseChatModel | None = None

        client_kwargs: dict[str, Any] = {"api_key": api_key, "base_url": base_url, "http_client": _shared_httpx_client()}
        client_cls = LangfuseAsyncOpenAI if is_langfuse_enabled() else OpenAIAsyncOpenAI
        if is_langfuse_enabled():
            get_langfuse_client()
        self._client = client_cls(**client_kwargs)

    # ── Agent 用：返回 LangChain BaseChatModel ──────

    def create_chat_model(self) -> BaseChatModel:
        """返回共享的 BaseChatModel 实例。

        bind_tools() 返回轻量 wrapper，不修改原对象，
        所以多个 stage/分支 可以安全共用同一个实例。
        """
        if self._chat_model is None:
            if self._provider == "qwen":
                self._chat_model = ChatOpenAI(
                    model=self._model,
                    api_key=self._api_key,
                    base_url=self._base_url,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    extra_body=self._extra_body,
                    http_async_client=_shared_httpx_client(),
                )
            else:
                self._chat_model = ChatDeepSeek(
                    model=self._model,
                    api_key=self._api_key,
                    base_url=self._base_url,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    extra_body=self._extra_body,
                    http_async_client=_shared_httpx_client(),
                )
        return self._chat_model

    # ── Judge / Compiler 用：直接 API 调用（带速率限制 + 重试）──

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        max_retries = settings.agent_llm_max_retries
        for attempt in range(max_retries):
            acquired = False
            try:
                await _rate_limiter.acquire(self._provider, self._model)
                acquired = True
                kwargs: dict[str, Any] = {
                    "model": self._model,
                    "messages": messages,
                    "temperature": temperature if temperature is not None else self._temperature,
                    "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                if self._extra_body is not None:
                    kwargs["extra_body"] = self._extra_body

                resp = await asyncio.wait_for(
                    self._client.chat.completions.create(**kwargs),
                    timeout=timeout,
                )
                break  # success
            except Exception as exc:
                is_transient = _is_transient_error(exc)
                if not is_transient and not isinstance(exc, TimeoutError):
                    raise
                if attempt >= max_retries - 1:
                    logger.error(
                        "LLM 调用失败（重试 {} 次后放弃）: model={}, provider={}, exc={}",
                        max_retries,
                        self._model,
                        self._provider,
                        exc.__class__.__name__,
                    )
                    raise
                delay = (1.0 * (2 ** attempt)) + random.uniform(0, 1.0)
                logger.warning(
                    "LLM 调用失败（{}/{}），{}s 后重试: model={}, provider={}, exc={}",
                    attempt + 1,
                    max_retries,
                    delay,
                    self._model,
                    self._provider,
                    exc.__class__.__name__,
                )
                await asyncio.sleep(delay)
            finally:
                if acquired:
                    _rate_limiter.release(self._provider, self._model)

        choice = resp.choices[0]
        result: dict[str, Any] = {
            "finish_reason": choice.finish_reason,
            "content": choice.message.content,
        }

        if choice.finish_reason == "tool_calls" or (choice.finish_reason == "stop" and choice.message.tool_calls):
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in choice.message.tool_calls
            ]

        return result

    async def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        resp = await self.chat(messages, temperature=temperature)
        return _extract_json(resp.get("content", ""))

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        return await self.chat(messages, tools=tools, temperature=temperature, max_tokens=max_tokens)
