"""HTTP 客户端弹性工具 - 处理高并发场景下的稳定性"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from typing import Any

from kbquant.client._base import QuantClientConnectionError, QuantClientHTTPError
from loguru import logger

_RETRYABLE_HTTP_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})


async def retry_api_call(fn: Callable[..., Any], name: str, task_id: str = "?", max_retries: int = 5) -> Any:
    """带重试的 API 调用，仅对瞬态错误重试。"""
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except QuantClientHTTPError as e:
            if e.status_code not in _RETRYABLE_HTTP_STATUSES or attempt >= max_retries:
                raise
            delay = 1.0 * (2 ** attempt) + random.uniform(0, 1.0)
            logger.warning("{} HTTP {} 重试 {}/{} (task={})，{:.1f}s 后重试",
                           name, e.status_code, attempt + 1, max_retries, task_id, delay)
            await asyncio.sleep(delay)
        except (asyncio.TimeoutError, QuantClientConnectionError, ConnectionRefusedError, ConnectionResetError, TimeoutError) as e:
            if attempt >= max_retries:
                logger.error("{} 网络错误 ({}) 重试已耗尽 {}/{} (task={})",
                           name, type(e).__name__, attempt + 1, max_retries, task_id)
                raise
            delay = 1.0 * (2 ** attempt) + random.uniform(0, 1.0)
            logger.warning("{} 网络错误 ({}) 重试 {}/{} (task={})，{:.1f}s 后重试",
                           name, type(e).__name__, attempt + 1, max_retries, task_id, delay)
            await asyncio.sleep(delay)


class AdaptiveRateLimiter:
    """自适应限流器 - 动态调整请求速率避免服务器过载"""

    def __init__(
        self,
        max_requests_per_second: int = 3000,
        window_size: float = 1.0,
        adaptive: bool = True,
    ) -> None:
        """
        Args:
            max_requests_per_second: 每秒最大请求数
            window_size: 时间窗口大小（秒）
            adaptive: 是否启用自适应降速（遇到错误时自动降低速率）
        """
        self.max_requests = max_requests_per_second
        self.window_size = window_size
        self.adaptive = adaptive

        self._requests: list[float] = []  # 请求时间戳
        self._lock = asyncio.Lock()
        self._error_count = 0
        self._last_error_time = 0.0
        self._current_limit = max_requests_per_second

    async def acquire(self) -> None:
        """获取请求许可（异步等待直到有配额）"""
        async with self._lock:
            now = time.time()

            # 清理过期的请求记录
            cutoff = now - self.window_size
            self._requests = [t for t in self._requests if t > cutoff]

            # 自适应降速：如果最近有错误，临时降低速率
            if self.adaptive and self._error_count > 0:
                # 5秒内有错误，降低到 50%
                if now - self._last_error_time < 5.0:
                    self._current_limit = self.max_requests // 2
                else:
                    # 逐步恢复
                    self._error_count = 0
                    self._current_limit = self.max_requests

            # 如果达到限流，等待
            if len(self._requests) >= self._current_limit:
                # 等待到最老的请求过期
                sleep_time = self._requests[0] + self.window_size - now
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                    # 递归重试
                    return await self.acquire()

            # 记录本次请求
            self._requests.append(now)

    def report_error(self) -> None:
        """报告请求错误（用于自适应降速）"""
        if self.adaptive:
            self._error_count += 1
            self._last_error_time = time.time()


def create_resilient_httpx_client(
    max_connections: int = 3000,
    max_keepalive_connections: int = 500,
    keepalive_expiry: float = 30.0,
    enable_http2: bool = False,
) -> dict:
    """创建高弹性的 httpx 客户端配置

    Args:
        max_connections: 最大并发连接数
        max_keepalive_connections: 最大保持活跃的连接数
        keepalive_expiry: keepalive 过期时间（秒）
        enable_http2: 是否启用 HTTP/2（可以多路复用，减少连接数）

    Returns:
        包含 limits 和 timeout 的配置字典
    """
    import httpx

    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
        keepalive_expiry=keepalive_expiry,  # 必须 < 服务端 idle timeout，否则取出僵尸连接导致 httpx.ReadError
    )

    timeout = httpx.Timeout(
        connect=5.0,    # 建立连接超时
        read=20.0,      # 读取响应超时（server PG statement_timeout=60s, 20s足够）
        write=10.0,     # 写入请求超时
        pool=10.0,      # 等待连接池超时（10s内拿不到连接快速失败）
    )

    return {
        "limits": limits,
        "timeout": timeout,
        "http2": enable_http2,  # HTTP/2 可以在单个连接上多路复用多个请求
    }
