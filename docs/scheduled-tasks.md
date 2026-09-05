# 定时任务调度器 (Scheduler)

## 概述

`Scheduler` 是一个轻量级定时任务调度器，实盘和模拟共用同一套 API。通过 `Clock` 获取时间（而非 `datetime.now()`），自动兼容两种运行模式。

- **实盘模式**：后台协程每秒检查一次，时间随真实时钟流逝
- **模拟模式**：在 Phase 1 tick 的 `clock.advance()` 后调用 `scheduler.tick()`，6h 粒度触发到期任务

## 基本用法

```python
from datetime import time, timedelta
from src.core.scheduler import Scheduler

# 创建（只需 Clock，不区分交易日）
scheduler = Scheduler(clock=clock)

# 每天定点执行
scheduler.daily("macro-daily", at=time(15, 30), fn=create_daily_report)

# 每隔固定时间
scheduler.every("heartbeat", interval=timedelta(minutes=30), fn=health_check)

scheduler.every("fast-poll", interval=timedelta(seconds=10), fn=fast_poll)
```

## 在 WorkflowApp 中注册新任务

在 `src/main.py` 的 `WorkflowApp.__init__` 中添加：

```python
self.scheduler.daily("macro-daily", at=time(9, 0), fn=self._on_macro_daily_scheduled)
self.scheduler.every("market-refresh", interval=timedelta(minutes=5), fn=self._refresh_market_data)
```

回调函数必须是 `async def` 且无参数：

```python
async def _refresh_market_data(self) -> None:
    self.market.refresh()
    logger.debug("行情数据已刷新")
```

## 原理

### 模拟模式

Phase 1 每次 tick 结束（`clock.advance()` 后）调用一次 `scheduler.tick()`。6h 的时钟跳跃区间内如有定时任务到期，会被捕获：

```
Tick N:  10:00 → clock.advance(+6h) → 16:00 → scheduler.tick()
                                                     now(16:00) >= next_run(15:30) → 触发
Tick N+1: 16:00 → clock.advance(+6h) → 22:00 → scheduler.tick()
                                                     now(22:00) >= next_run(次日15:30) → 未到期
```

不区分交易日 —— 交易日判断是 Phase 2 `_replay_triggers_in_window` 的职责。

### 实盘模式

`scheduler.run()` 作为独立后台协程运行，每秒调用一次 `tick()`：

```python
# src/main.py start() 中
asyncio.create_task(self.scheduler.run())
```

## API 参考

### `Scheduler(clock)`

| 参数 | 说明 |
|------|------|
| `clock` | `Clock` 实例，调度器从中获取当前时间 |

### `scheduler.daily(name, at, fn)`

每日定点执行。

| 参数 | 说明 |
|------|------|
| `name` | 任务名称（日志中用） |
| `at` | `datetime.time`，执行时刻 |
| `fn` | `async def fn() -> None` 回调 |

### `scheduler.every(name, interval, fn)`

固定间隔执行。

| 参数 | 说明 |
|------|------|
| `name` | 任务名称 |
| `interval` | `datetime.timedelta`，间隔 |
| `fn` | `async def fn() -> None` 回调 |

### `await scheduler.tick()`

检查并触发所有到期任务。模拟模式中嵌入回放循环调用，实盘模式由 `run()` 内部调用。

### `await scheduler.run()`

实盘模式专用：后台自循环，每秒 `tick()` 一次。通过 `asyncio.create_task()` 启动。

## 示例：添加一个收盘前仓位检查

```python
# 在 WorkflowApp.__init__ 中
from datetime import time

self.scheduler.daily("pre-close-check", at=time(14, 55), fn=self._check_positions_before_close)

# 回调
async def _check_positions_before_close(self) -> None:
    """收盘前 5 分钟检查仓位风险"""
    positions = await self.quant.trading.list(status="open")
    for p in positions:
        if p.unrealized_pnl_pct < -settings.max_drawdown_pct:
            logger.warning("仓位 {} 触及止损线", p.symbol)
            # 触发平仓...
```

## 示例：每 10 分钟推送一次行情摘要

```python
from datetime import timedelta

self.scheduler.every("market-summary", interval=timedelta(minutes=10), fn=self._push_market_summary)

async def _push_market_summary(self) -> None:
    indices = await self.market.get_index_prices(["000001.SH", "399001.SZ"])
    logger.info("行情摘要 | SH={}, SZ={}", indices.get("000001.SH"), indices.get("399001.SZ"))
```
