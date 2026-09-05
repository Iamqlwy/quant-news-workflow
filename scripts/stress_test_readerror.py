"""raw_information 读取压测 —— 固定并发 1000，使用 QuantClient。
不做重试，直接观察原始 ReadError 数量。
配置与 components.py create_resilient_httpx_client 完全一致。
"""

import asyncio
import random
import time
from dataclasses import dataclass, field

import httpx

from kbquant.client import QuantClient, QuantClientConnectionError

BASE_URL = "http://localhost:8000"
CONCURRENCY = 1000
ID_POOL_SIZE = 200
CLIENT_COUNT = 5

# ---- 与 components.py create_resilient_httpx_client 完全一致的配置 ----
MAX_CONNECTIONS = 500
MAX_KEEPALIVE_CONNECTIONS = 20
KEEPALIVE_EXPIRY = 4.0
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=None)
LIMITS = httpx.Limits(
    max_connections=MAX_CONNECTIONS,
    max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
    keepalive_expiry=KEEPALIVE_EXPIRY,
)


@dataclass
class StressResult:
    concurrency: int
    ok: int
    fail: int
    total_time_s: float
    latencies_s: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def qps(self) -> float:
        return self.concurrency / self.total_time_s if self.total_time_s > 0 else 0.0

    @property
    def p50(self) -> float:
        return _percentile(self.latencies_s, 0.50)

    @property
    def p95(self) -> float:
        return _percentile(self.latencies_s, 0.95)

    @property
    def p99(self) -> float:
        return _percentile(self.latencies_s, 0.99)

    @property
    def min_lat(self) -> float:
        return min(self.latencies_s) if self.latencies_s else 0.0

    @property
    def max_lat(self) -> float:
        return max(self.latencies_s) if self.latencies_s else 0.0


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    idx = (len(xs) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(xs) - 1)
    frac = idx - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


async def fetch_id_pool(client: QuantClient, count: int) -> list[str]:
    page_size = min(count, 100)
    resp = await client.information.list(page=1, page_size=page_size)
    if len(resp.items) < count:
        remaining = min(count - len(resp.items), 100)
        resp2 = await client.information.list(page=2, page_size=remaining)
        resp.items.extend(resp2.items)
    ids = [item["id"] for item in resp.items]
    print(f"[setup] fetched {len(ids)} IDs from information.list")
    return ids


async def one_get(client: QuantClient, info_id: str) -> tuple[bool, float, str]:
    t0 = time.perf_counter()
    try:
        await client.information.get(info_id)
        elapsed = time.perf_counter() - t0
        return True, elapsed, ""
    except QuantClientConnectionError as e:
        elapsed = time.perf_counter() - t0
        return False, elapsed, str(e)[:120]
    except Exception as e:
        elapsed = time.perf_counter() - t0
        return False, elapsed, f"{type(e).__name__}: {str(e)[:120]}"


def _make_client() -> QuantClient:
    return QuantClient(
        BASE_URL,
        limits=LIMITS,
        timeout=TIMEOUT,
    )


async def run_stress(ids: list[str]) -> StressResult:
    clients = [_make_client() for _ in range(CLIENT_COUNT)]

    t0 = time.perf_counter()
    tasks = [
        one_get(clients[i % CLIENT_COUNT], random.choice(ids))
        for i in range(CONCURRENCY)
    ]
    results = await asyncio.gather(*tasks)
    total_time = time.perf_counter() - t0

    close_tasks = [c.close() for c in clients]
    await asyncio.gather(*close_tasks)

    report = StressResult(concurrency=CONCURRENCY, ok=0, fail=0, total_time_s=total_time)
    for ok, lat, err in results:
        if ok:
            report.ok += 1
            report.latencies_s.append(lat)
        else:
            report.fail += 1
            report.errors.append(err)
    return report


async def main() -> None:
    print("=" * 80)
    print(f"raw_information 读取压测 — 目标: {BASE_URL}")
    print(f"并发: {CONCURRENCY}  |  客户端: {CLIENT_COUNT} x {MAX_CONNECTIONS}连接")
    print(f"keepalive: {MAX_KEEPALIVE_CONNECTIONS}  |  keepalive_expiry: {KEEPALIVE_EXPIRY}s  |  pool: None")
    print(f"connect={TIMEOUT.connect}s  read={TIMEOUT.read}s  write={TIMEOUT.write}s  |  无重试")
    print("请求: client.information.get(id)")
    print("=" * 80)

    async with _make_client() as setup_client:
        id_pool = await fetch_id_pool(setup_client, ID_POOL_SIZE)

    if not id_pool:
        print("[ERROR] 未能获取到任何 raw_information ID，请确认数据已录入。")
        return

    report = await run_stress(id_pool)

    print()
    print(f"{'='*80}")
    print(f"结果汇总")
    print(f"{'='*80}")
    print(f"  总并发: {report.concurrency}")
    print(f"  成功:   {report.ok}")
    print(f"  失败:   {report.fail}")
    print(f"  总耗时: {report.total_time_s:.2f}s")
    print(f"  QPS:    {report.qps:.1f}")
    print(f"  --- 延迟分布 (成功请求) ---")
    if report.latencies_s:
        print(f"  p50:    {report.p50*1000:8.1f} ms")
        print(f"  p95:    {report.p95*1000:8.1f} ms")
        print(f"  p99:    {report.p99*1000:8.1f} ms")
        print(f"  min:    {report.min_lat*1000:8.1f} ms")
        print(f"  max:    {report.max_lat*1000:8.1f} ms")

    if report.errors:
        error_counts: dict[str, int] = {}
        for err in report.errors:
            key = err[:60]
            error_counts[key] = error_counts.get(key, 0) + 1
        print(f"\n  --- 错误分布 ---")
        for msg, count in sorted(error_counts.items(), key=lambda x: -x[1])[:10]:
            print(f"  [{count:>4}] {msg}")

    print()
    print("done.")


if __name__ == "__main__":
    asyncio.run(main())
