"""爬虫频率压力测试 —— 找出各数据源被封禁的临界值。"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    )
}

# 各数据源的真实 API 端点
SOURCES: dict[str, str] = {
    "em": "https://np-weblist.eastmoney.com/comm/web/getFastNewsList",
    "sina": "https://zhibo.sina.com.cn/api/zhibo/feed",
    "futu": "https://news.futunn.com/news-site-api/main/get-flash-list",
    "ths": "https://news.10jqka.com.cn/tapp/news/push/stock",
    "cls": "https://www.cls.cn/api/cache?app=CailianpressWeb&name=telegraph&os=web&sv=8.7.9",
}


def _hit(name: str, url: str) -> dict:
    """单次请求，返回状态和耗时。"""
    t0 = time.perf_counter()
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        elapsed = time.perf_counter() - t0
        try:
            data = resp.json()
            item_count = len(data.get("data", {}).get("roll_data", []))
        except Exception:
            item_count = -1
        return {
            "source": name,
            "status": resp.status_code,
            "elapsed": round(elapsed, 3),
            "items": item_count,
            "blocked": False,
        }
    except requests.Timeout:
        return {"source": name, "status": 0, "elapsed": 15, "items": 0, "blocked": True}
    except Exception as exc:
        return {
            "source": name,
            "status": -1,
            "elapsed": round(time.perf_counter() - t0, 3),
            "items": 0,
            "blocked": True,
            "error": str(exc)[:80],
        }


def _test_interval(name: str, url: str, interval: float, rounds: int = 10) -> dict:
    """对单个数据源以固定间隔连续请求 rounds 次。"""
    results: list[dict] = []
    blocked_at = None

    for i in range(rounds):
        result = _hit(name, url)
        results.append(result)

        if result["blocked"] or result["status"] in (403, 429, 503):
            blocked_at = i + 1
            # 被 ban 后停止
            break

        if i < rounds - 1:
            time.sleep(interval)

    success = [r for r in results if not r["blocked"] and r["status"] == 200]
    avg_elapsed = (
        round(sum(r["elapsed"] for r in success) / len(success), 3) if success else 0
    )

    return {
        "source": name,
        "interval": interval,
        "rounds_planned": rounds,
        "rounds_done": len(results),
        "success_count": len(success),
        "blocked_at": blocked_at,
        "avg_elapsed": avg_elapsed,
        "status_codes": list(set(r["status"] for r in results)),
        "results": results,
    }


def _test_concurrent(name: str, url: str, workers: int) -> dict:
    """对单个数据源同时发起 workers 个请求。"""
    t0 = time.perf_counter()
    results: list[dict] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_hit, name, url): i for i in range(workers)}
        for future in as_completed(futures):
            results.append(future.result())

    total_elapsed = round(time.perf_counter() - t0, 3)
    success = [r for r in results if not r["blocked"] and r["status"] == 200]

    return {
        "source": name,
        "concurrent": workers,
        "total_elapsed": total_elapsed,
        "success_count": len(success),
        "fail_count": len(results) - len(success),
        "status_codes": list(set(r["status"] for r in results)),
        "blocked_any": any(r["blocked"] for r in results),
        "results": results,
    }


# ---------------------------------------------------------------------------
# 主测试流程
# ---------------------------------------------------------------------------


def run_interval_tests() -> list[dict]:
    """按不同间隔测试所有数据源。"""
    intervals = [0, 0.1, 0.2, 0.5, 1.0, 2.0]
    all_results: list[dict] = []

    for name, url in SOURCES.items():
        print(f"\n{'='*60}")
        print(f"测试 [{name}]  —— 串行间隔请求")
        print(f"{'='*60}")
        for iv in intervals:
            label = f"间隔={iv}s" if iv > 0 else "无间隔"
            print(f"  {label} ... ", end="", flush=True)
            result = _test_interval(name, url, iv, rounds=10)
            status = "OK" if result["blocked_at"] is None else f"BLOCKED at round {result['blocked_at']}"
            print(f"{result['success_count']}/10 成功, avg {result['avg_elapsed']}s, [{status}]")
            all_results.append(result)

            # 如果无间隔就被封，跳过更大间隔
            if iv == 0 and result["blocked_at"] is not None:
                pass  # 仍然测试有间隔的情况，因为可能间隔后恢复正常

    return all_results


def run_concurrent_tests() -> list[dict]:
    """测试各数据源的并发能力。"""
    worker_levels = [2, 5, 10]
    all_results: list[dict] = []

    for name, url in SOURCES.items():
        print(f"\n{'='*60}")
        print(f"测试 [{name}]  —— 并发请求")
        print(f"{'='*60}")
        for w in worker_levels:
            print(f"  并发={w} ... ", end="", flush=True)
            result = _test_concurrent(name, url, w)
            status = "BLOCKED" if result["blocked_any"] else "OK"
            codes = result["status_codes"]
            print(f"{result['success_count']}/{w} 成功, {result['total_elapsed']}s, codes={codes} [{status}]")
            all_results.append(result)

    return all_results


def run_warmup_test() -> list[dict]:
    """对每个源先请求一次，确保基本连通性，然后逐步提速测试。

    采用逐步缩间隔策略：2s → 1s → 0.5s → 0.2s → 0.1s → 0s
    """
    all_results: list[dict] = []
    for name, url in SOURCES.items():
        print(f"\n{'='*60}")
        print(f"测试 [{name}]  —— 逐步提速 (warmup)")
        print(f"{'='*60}")

        # 先做一次预热
        warm = _hit(name, url)
        print(f"  预热: status={warm['status']}, items={warm['items']}")

        if warm["status"] != 200:
            print(f"  SKIP: 预热失败")
            continue

        for iv in [2.0, 1.0, 0.5, 0.2, 0.1, 0]:
            label = f"间隔={iv}s" if iv > 0 else "无间隔"
            print(f"  {label} 连发20次 ... ", end="", flush=True)
            result = _test_interval(name, url, iv, rounds=20)
            status = "OK" if result["blocked_at"] is None else f"BLOCKED at round {result['blocked_at']}"
            print(f"{result['success_count']}/20, [{status}]")
            all_results.append(result)
            # 逐步降间隔，即使被封也继续尝试下一步

    return all_results


if __name__ == "__main__":
    print("=" * 60)
    print("爬虫频率压力测试")
    print("=" * 60)

    print("\n\n### 阶段 1: 逐步提速测试 ###")
    stage1 = run_warmup_test()

    print("\n\n### 阶段 2: 并发测试 ###")
    stage2 = run_concurrent_tests()

    # 报告摘要
    print("\n\n" + "=" * 60)
    print("测试摘要")
    print("=" * 60)

    for r in stage1:
        iv = r["interval"]
        label = f"间隔={iv}s" if iv > 0 else "无间隔"
        blocked = r["blocked_at"] is not None
        flag = "BLOCKED" if blocked else "SAFE"
        print(f"  {r['source']:6s} {label:10s}: {r['success_count']:2d}/{r['rounds_planned']:2d} [{flag}]")

    print()
    for r in stage2:
        blocked = r["blocked_any"]
        flag = "BLOCKED" if blocked else "SAFE"
        print(f"  {r['source']:6s} 并发={r['concurrent']:2d}  : {r['success_count']:2d}/{r['concurrent']:2d} [{flag}]")
