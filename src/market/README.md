# market_new —— 行情数据模块（重构版）

## 原有设计的主要问题

1. **服务定位器反模式**：`MarketContext` 打包所有依赖，隐藏真实依赖关系，难以测试
2. **日期格式混乱**：YYYYMMDD / YYYY-MM-DD / datetime64 / 整数混用，频繁转换
3. **单位转换在错误的层级**：归一化在 `session_builder.py` 而非 loader，直接调用 loader 会得到错误单位
4. **O(N²) Tick 聚合**：`_write_bar` 每次 flush 都 `pd.concat`，240 次/天/股
5. **重复遍历**：`compute_market_breadth` 和 `_intraday_market_snapshot_from_1m` 逻辑几乎相同
6. **薄透传类**：`Resolver`、`MarketRepository`、`CsvSource` 几乎没有实际逻辑
7. **中文列名硬编码在 6 个文件**中
8. **没有 TypedDict 返回类型**：全部裸 dict，编译期无类型检查
9. **冗余数据结构**：4 个字典表达同一份指数映射
10. **Clock 类型为 `object`**：无类型安全

## 新架构说明

```
market_new/
├── __init__.py              # 公开 API：MarketDataProvider
├── config.py                # 常量（只读）
├── types.py                 # TypedDict / Dataclass 类型
├── provider.py              # MarketDataProvider 门面
├── session_builder.py       # Session 数据构建器
├── services/                # 领域服务层
│   ├── bars.py              # OHLCV 查询 + 时钟截断
│   ├── price.py             # 实时价格
│   ├── indicator.py         # 技术指标
│   ├── sector.py            # 板块分析
│   ├── breadth.py           # 市场宽度
│   ├── snapshot.py          # 快照
│   ├── history.py           # 历史数据
│   ├── limit.py             # 涨跌停
│   └── resolver.py          # 名称解析
├── data/                    # 数据层
│   ├── cache.py             # 三层缓存管理
│   ├── loader.py            # CSV 加载
│   ├── normalizer.py        # 列归一化 + 单位转换
│   └── indexer.py           # 二进制索引
├── compute/                 # 计算层（纯函数）
│   ├── indicators.py        # 指标计算
│   ├── indicator_state.py   # O(1) 增量更新
│   ├── bars.py              # Bar 合成
│   ├── sector.py            # 板块 Bar 预计算
│   ├── tick_agg.py          # Tick 聚合（list-buffer）
│   └── resample.py          # 重采样
├── live/                    # 实时层
│   └── xt_provider.py       # xtquant 连接
├── tests/                   # 测试
│   ├── test_clock.py         # 测试统一 Clock
│   ├── test_indicators.py
│   ├── test_bars.py
│   ├── test_cache.py
│   ├── test_tick_agg.py
│   ├── test_sector.py
│   ├── test_snapshot.py
│   └── test_performance.py
├── README.md
└── REFACTOR_REPORT.md
```

## 核心数据流

```
MarketDataProvider.refresh()
  └─> session_builder.build_session()
        ├─> data/loader.py  (CSV → 归一化 DataFrame)
        ├─> data/indexer.py (1m 批量索引读取)
        ├─> compute/sector.py (板块 Bar 预计算)
        └─> SessionData (只读缓存)

MarketDataProvider.get_bars()
  └─> services/bars.py
        ├─> data/cache.py (读取缓存)
        ├─> compute/bars.py (合成实时日线)
        └─> 时钟截断

MarketDataProvider.get_realtime_price()
  └─> services/price.py
        ├─> data/cache.py (tick 缓存 或 1m 数据)
        └─> compute/bars.py (前收盘查找)

MarketDataProvider.get_technical_indicators_cached()
  └─> services/indicator.py
        ├─> Cycle 缓存 → 增量更新 → 全量计算
        └─> compute/indicators.py + compute/indicator_state.py
```

## 重要设计决策

1. **加载时归一化**：所有数据在进入 SessionData 前完成列名归一化和单位转换（万元/万股），不再在服务层做转换
2. **显式依赖注入**：每个服务构造函数接受具体的依赖对象，不使用服务定位器
3. **列表缓冲 Tick 聚合**：`list[dict]` 替代 `pd.concat` 循环，消除 O(N²)
4. **单一日期表示**：内部使用 `pd.Timestamp`，字符串转换仅在 I/O 边界
5. **TypedDict 返回类型**：所有服务方法返回 TypedDict/dataclass 而非裸 dict
6. **类型化 Clock**：统一使用 `src.core.clock.Clock`（已合并，删除原 TradingClock ABC）
7. **单一指数映射**：`_INDEX_DEFS` → 推导其他视图，消除冗余

## 新旧接口映射

| 旧接口 (MarketDataProvider) | 新接口 | 说明 |
|---|---|---|
| `get_bars(ticker, granularity, start, end, category)` | 相同 | 保持兼容 |
| `get_concept_kline(...)` | 相同 | |
| `get_realtime_price(s)(ticker)` | 相同 | 返回类型改为 PriceDict |
| `get_technical_indicators_cached(ticker)` | 相同 | 返回类型改为 IndicatorDict |
| `get_turnover_rate(ticker)` | 相同 | |
| `get_zdt_record(ticker)` | 相同 | 返回类型改为 ZdtRecordDict |
| `get_zdt_follow_through()` | 新增 | 昨日涨停股今日表现 |
| `get_sector_overview(sector)` | 相同 | 返回类型改为 SectorOverviewDict |
| `get_sector_overview_cached(sector)` | 相同 | |
| `get_sector_intraday(sector_code)` | 相同 | |
| `get_sector_leader(sector_code)` | 相同 | |
| `get_sector_volume_ratio(sector_code, n)` | 相同 | |
| `get_concept_list(con_type)` | 相同 | |
| `get_concept_members(con_code)` | 相同 | |
| `get_stock_concepts(ticker)` | 相同 | |
| `get_market_breadth()` | 相同 | 返回类型改为 MarketBreadthDict |
| `get_index_overview()` | 相同 | |
| `get_today_market_summary()` | 相同 | |
| `get_market_snapshot(date)` | 相同 | 返回类型改为 SnapshotDict |
| `get_intraday_snapshot_cached(ticker)` | 相同 | |
| `get_price_history(ticker, from, to)` | 相同 | |
| `resolve_*` | 相同 | |
| `refresh()` | 相同 | |
| `truncate_for_clock(df, is_intraday)` | 移除 | 改为 BarService 内部方法 |
| `get_classification()` | 相同 | |
| `get_sector_bars(code)` | 相同 | |
| `get_daily_df(ticker)` | 相同 | |
| `get_klines_path()` | `klines_path` 属性 | |
| `clock` | `clock` 属性 | 类型改为 Clock |
| `trading_days` | 相同 | |

## 使用示例

```python
from datetime import datetime, timedelta

from src.core.clock import Clock, TimeConfig
from src.market import MarketDataProvider

# 模拟模式
clock = Clock(TimeConfig(
    start_time=datetime(2025, 1, 15, 10, 30, 0),
    tick_duration=timedelta(minutes=1),
    realtime=False,
))
provider = MarketDataProvider(klines_path="/data/klines", clock=clock)

# 获取 K 线
bars = provider.get_bars("000001.SZ", granularity="1d")

# 获取实时价格
price = await provider.get_realtime_price("000001.SZ")

# 获取技术指标
indicators = provider.get_technical_indicators_cached("000001.SZ")
print(indicators["ma5"], indicators["rsi14"])

# 获取市场宽度
breadth = await provider.get_market_breadth()
print(f"上涨: {breadth['up_count']}, 下跌: {breadth['down_count']}")

# 获取涨跌停
zdt = provider.get_zdt_record("000001.SZ")
if zdt and zdt["is_limit"]:
    print(f"{zdt['tag']} {zdt['board_type']}")

# 生命周期
provider.refresh()
# ... 交易逻辑 ...
```

## 迁移方式

1. 将 `from src.market import MarketDataProvider` 改为 `from market_new import MarketDataProvider`
2. `provider = MarketDataProvider()` → `provider = MarketDataProvider(klines_path="/path/to/data")`
3. 时钟统一使用 `src.core.clock.Clock`：通过 `TimeConfig` 配置实盘/模拟模式，详见 `src/core/clock.py`
4. 返回类型从裸 dict 改为 TypedDict，使用方式不变（TypedDict 兼容 dict 访问）
5. `truncate_for_clock` 不再对外暴露，O/LCV 查询已内置时钟截断
