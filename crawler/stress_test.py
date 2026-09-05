"""爬虫 1 小时压力测试 —— 5 个源各自独立循环，每轮间隔 0.3-1s。"""

from __future__ import annotations

import random
import time
import threading
from datetime import datetime, timedelta

from crawler import crawl_cls, crawl_em, crawl_futu, crawl_sina, crawl_ths
from crawler.dedup import get_dedup_store

SOURCES = {
    "em": crawl_em,
    "sina": crawl_sina,
    "futu": crawl_futu,
    "ths": crawl_ths,
    "cls": crawl_cls,
}


def _run_source(name: str, crawler, end_time: datetime, results: dict):
    """单个源独立循环爬取，直到 end_time。"""
    dedup = get_dedup_store()
    round_num = 0
    total_items = 0
    failures = 0
    times: list[float] = []

    while datetime.now() < end_time:
        delay = random.uniform(0.3, 1.0)
        time.sleep(delay)

        t0 = time.perf_counter()
        try:
            items = crawler(dedup=dedup)
            elapsed = time.perf_counter() - t0
            times.append(elapsed)
            n = len(items)
            if n == 0:
                failures += 1
            total_items += n
            round_num += 1
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            times.append(elapsed)
            failures += 1
            round_num += 1
            if failures <= 3:
                print(f"  [{name}] round {round_num} 异常: {exc}")

    results[name] = {
        "rounds": round_num,
        "total_items": total_items,
        "failures": failures,
        "avg_time": sum(times) / max(len(times), 1),
    }


def run_stress_test(duration_minutes: int = 60):
    start_time = datetime.now()
    end_time = start_time + timedelta(minutes=duration_minutes)

    print(f"开始压力测试: {start_time:%H:%M:%S} -> {end_time:%H:%M:%S}", flush=True)
    print(f"模式: 5 源独立线程, 每轮 sleep 0.3-1s 后立即再爬", flush=True)
    print(f"{'='*70}", flush=True)

    results: dict = {}
    threads = []
    for name, crawler in SOURCES.items():
        t = threading.Thread(target=_run_source, args=(name, crawler, end_time, results))
        t.start()
        threads.append(t)

    # 每分钟打印进度
    while any(t.is_alive() for t in threads):
        time.sleep(60)
        elapsed = (datetime.now() - start_time).total_seconds()
        remaining = (end_time - datetime.now()).total_seconds()
        print(f"[{datetime.now():%H:%M:%S}] 已运行 {elapsed/60:.0f}m  剩余 {remaining/60:.0f}m")

    for t in threads:
        t.join()

    # ---- 报告 ----
    duration = (datetime.now() - start_time).total_seconds()
    print()
    print(f"{'='*70}")
    print("压力测试报告")
    print(f"{'='*70}")
    print(f"总耗时:     {duration/60:.1f} 分钟")
    print()

    total_rounds = 0
    total_items = 0
    total_failures = 0
    for source in ["em", "sina", "futu", "ths", "cls"]:
        r = results.get(source, {})
        rounds = r.get("rounds", 0)
        items = r.get("total_items", 0)
        fails = r.get("failures", 0)
        avg_t = r.get("avg_time", 0)
        fail_rate = fails / max(rounds, 1) * 100
        rpm = rounds / duration * 60
        print(
            f"  {source:6s}: {rounds:5d} 轮  {items:6d} 条  "
            f"{fails:4d} 失败 ({fail_rate:.1f}%)  "
            f"avg {avg_t:.2f}s  {rpm:.1f} 轮/分钟"
        )
        total_rounds += rounds
        total_items += items
        total_failures += fails

    print()
    print(f"  合计:   {total_rounds:5d} 轮  {total_items:6d} 条  {total_failures} 失败")
    print(f"  总频率: {total_rounds / duration * 60:.1f} 轮/分钟")
    print()
    if total_failures == 0:
        print("结论: 全部通过, 无失败!")
    else:
        print(f"结论: {total_failures}/{total_rounds} 失败 ({total_failures / max(total_rounds, 1) * 100:.2f}%)")


if __name__ == "__main__":
    run_stress_test(duration_minutes=60)
