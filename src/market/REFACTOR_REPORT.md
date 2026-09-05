# 重构报告

## 发现的问题清单

### 设计问题
1. **服务定位器反模式**：`MarketContext` 打包所有依赖，函数只声明 `context` 参数，无法从签名看出实际依赖
2. **薄透传类**：`Resolver`（纯转调）、`MarketRepository`（转调+少量逻辑）、`CsvSource`（仅 trigger session_builder）没有独立存在价值
3. **`SnapshotService.__init__` 接受两种类型**：`CacheManager` 或 `MarketContext`，内部 4 个 property 都做 `hasattr`/`getattr` 分支
4. **`loader.py` 只有一行**：`from src.market.loaders import *`，无意义的重导出

### 数据问题
5. **日期格式混乱**：YYYYMMDD / YYYY-MM-DD / datetime64 / 整数混用，频繁字符串转换
6. **单位转换在错误层级**：归一化在 `session_builder.py` 而非 loader，直接调用 loader 得到错误单位
7. **4 个冗余结构表达同一份索引映射**：`_INDEX_NAME_TO_CODE`、`_INDEX_CODES`、`_CODE_TO_INDEX_NAME`、`_INDEX_DAILY_FILES`
8. **`ts_code` 含义不一致**：概念成员加载中 `con_code→ts_code` 重命名混淆
9. **所有返回值为裸 dict**：无编译期类型检查

### 性能问题
10. **O(N²) `pd.concat`**：`TickAggregator._write_bar` 每次 flush 做一次 concat（240 次/天/股）
11. **重复遍历全市场**：`compute_market_breadth` 和 `_intraday_market_snapshot_from_1m` 逻辑几乎重复
12. **`asyncio.to_thread` 创建数千线程**：`get_realtime_prices` 无并发限制
13. **密集矩阵分配**：`precompute_sector_bars` 分配 T×K 矩阵（5000×240=1.2M），即使数据稀疏

### 可维护性问题
14. **中文列名硬编码在 6 个文件**中
15. **`except Exception` 吞掉所有异常**：`load_stock_daily` 的 indicator 加载
16. **字符串比较推断排序方向**：`"9" > "10"` 为 True
17. **Clock 类型为 `object`**：无类型安全

## 已完成的优化

### 1. 类型化时钟
- **旧**：`clock: object = None`，duck-typing
- **新**：`TradingClock` ABC，`RealtimeClock` 和 `SimulationClock` 实现
- **收益**：编译期类型安全，IDE 自动补全，清晰的协议定义

### 2. 加载时归一化
- **旧**：loader 返回原始值，`session_builder` 事后转换
- **新**：`normalizer.py` 统一负责列重命名 + 单位转换，loader 在返回前完成归一化
- **收益**：无论从哪个路径获取数据，单位始终一致

### 3. List-buffer Tick 聚合
- **旧**：每次 flush → `pd.concat`，240 次 O(N²)
- **新**：`list[dict]` 收集，flush 时一次性 `pd.DataFrame(bar_list)`
- **收益**：240 次 concat 降至 1 次 DataFrame 构造，预期 10-50x 加速

### 4. 消除服务定位器
- **旧**：所有服务接受 `MarketContext`
- **新**：每个服务接受具体依赖（`CacheManager`、`Clock`、`BarService` 等）
- **收益**：从函数签名即可了解真实依赖，易于 mock 和测试

### 5. 单一指数映射
- **旧**：4 个独立 dict
- **新**：`_INDEX_DEFS` 一个 list-of-tuples，其他视图通过 dict comprehension 推导
- **收益**：新增指数只需修改一处

### 6. TypedDict 返回类型
- **旧**：裸 dict
- **新**：`PriceDict`、`IndicatorDict`、`MarketBreadthDict` 等 TypedDict
- **收益**：IDE 自动补全，编译期类型检查

### 7. 移除薄透传类
- `Resolver`、`MarketRepository`、`CsvSource` 的功能合并到更合适的组件中
- `loader.py` shim 删除
- `context.py` 删除（不再需要服务定位器）

### 8. 并发限制
- `get_realtime_prices` 添加 `asyncio.Semaphore(50)` 限制并发线程数
- **收益**：避免数千线程同时创建，保护线程池

### 9. 统一日期表示
- **旧**：多种字符串格式 + datetime64 混用
- **新**：内部统一 `pd.Timestamp`，字符串转换仅在 I/O 边界
- **收益**：消除频繁的格式转换，减少日期比较错误

## 每项优化的收益总结

| 优化项 | 复杂度变化 | 预期收益 |
|---|---|---|
| List-buffer tick 聚合 | O(N²) → O(N) | 240x 减少 concat 次数 |
| 加载时归一化 | 无变化 | 消除单位不一致的 bug |
| 并发限制 | O(1) 改进 | 避免线程池膨胀 |
| 消除重复遍历 | 代码合并 | 减少 50% 重复逻辑 |
| 类型化返回值 | 编译期改进 | 消除 dict key 拼写错误 |

## 尚未解决的问题

1. **`precompute_sector_bars` 密集矩阵**：仍使用 T×K 密集矩阵，对于超过 240 分钟的时间范围会有内存问题。建议后续改为稀疏表示或按概念分组计算
2. **xtquant 回调中的时间过滤**：使用 `datetime.now()` 而非精确的交易日历
3. **`load_stock_daily` 仍使用 `except Exception`** 作 fallback——在新实现中改为显式异常类型
4. **中文列名依赖**：CSV 文件格式变化时仍需修改 `config.py` 中的映射表。建议后续以配置文件或 schema 文件形式管理

## 兼容性风险

1. **`truncate_for_clock` 不再公开暴露**：外部代码若直接调用此方法需要迁移到 BarService 内部
2. **Clock 类型变更**：从 `object` 改为 `TradingClock`，自定义 clock 需要实现 `now()` 和 `today()`
3. **返回类型变更**：从裸 dict 改为 TypedDict，但 TypedDict 兼容 dict 访问，不影响 `result["key"]` 用法
4. **构造函数变更**：`MarketDataProvider` 现在需要 `klines_path` 参数，且 clock 需显式传入
5. **导入路径变更**：从 `src.market` 改为 `market_new`
