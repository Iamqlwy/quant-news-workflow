# 高并发 HTTP 客户端配置指南

## 当前配置

系统配置为支持 **1 秒内 3000 个并发 search 请求**。

### 连接池参数

```python
max_connections=3000           # 最大并发连接数
max_keepalive_connections=500  # 保持活跃的连接数
keepalive_expiry=30.0          # keepalive 过期时间（秒）
pool_timeout=None              # 不限制等待连接池时间
```

### 系统要求

- **操作系统**: Windows (当前 ulimit=3200，留 200 余量)
- **文件描述符**: 至少 3200+
- **内存**: 每个连接约 100-200KB，3000 连接需要 300-600MB

## 避免 HTTP 崩溃的关键设计

### 1. 连接池排队而非失败
- `pool=None` 确保当连接池满时，请求会**排队等待**而不是立即失败
- 这避免了大量 `PoolTimeout` 错误

### 2. Keepalive 连接复用
- 保持 500 个长连接活跃，显著减少 TCP 握手开销
- 30 秒过期时间防止占用过多空闲连接

### 3. 合理的超时配置
```python
connect=10.0   # 建立连接超时
read=30.0      # 读取响应超时
write=10.0     # 写入请求超时
```

## 可选：自适应限流器

如果仍然遇到服务器过载，可以在应用层添加限流：

```python
from src.utils.http_resilience import AdaptiveRateLimiter

# 创建限流器（每秒最多 3000 个请求）
limiter = AdaptiveRateLimiter(
    max_requests_per_second=3000,
    adaptive=True,  # 遇到错误时自动降速
)

# 在发起请求前获取许可
async def make_request():
    await limiter.acquire()
    # 发起 HTTP 请求
    response = await client.search(...)
    return response
```

### 自适应降速机制
- 检测到连续错误时，自动降低到 50% 速率
- 5 秒内无错误后逐步恢复到正常速率

## 监控建议

### 1. 连接池使用率
```python
# 如果 QuantClient 暴露连接池统计
print(f"Active connections: {client._pool_stats.active}")
print(f"Idle connections: {client._pool_stats.idle}")
```

### 2. 请求失败率
- 监控 `ConnectionError`、`TimeoutError`、`PoolTimeout`
- 如果失败率 > 1%，考虑启用自适应限流

### 3. 响应时间
- P50 应该 < 100ms
- P99 应该 < 1000ms
- 如果 P99 > 5s，说明服务器过载

## 故障排查

### 问题 1: `PoolTimeout` 错误
**原因**: 连接池满且有请求超过 pool timeout

**解决**: 已设置 `pool=None`，不应该出现此错误

### 问题 2: `ConnectionError: Too many open files`
**原因**: 系统文件描述符限制不足

**解决**:
```bash
# Linux/Mac
ulimit -n 5000

# Windows: 通常无需调整（默认足够高）
```

### 问题 3: 服务器返回 429 或 503
**原因**: 服务器端过载

**解决**: 启用自适应限流器，在应用层控制速率

### 问题 4: 内存占用过高
**原因**: 3000 个连接占用大量内存

**解决**: 
- 降低 `max_keepalive_connections` 到 300
- 降低 `keepalive_expiry` 到 15 秒

## 性能优化建议

### 短期优化（已实现）
- ✅ 增加连接池到 3000
- ✅ 启用 keepalive 连接复用
- ✅ 移除 pool timeout 限制

### 长期优化（未实现）
- ⏳ 启用 HTTP/2（需要 QuantClient 支持）
- ⏳ 实现请求批处理（多个 search 合并为一个请求）
- ⏳ 添加本地缓存（减少重复请求）
- ⏳ 服务器端实现限流和负载均衡

## 测试验证

```python
import asyncio
import time
from src.runners.components import Components

async def stress_test():
    components = Components(...)
    client = components.quant_client
    
    # 并发发送 3000 个请求
    start = time.time()
    tasks = [client.search(...) for _ in range(3000)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.time() - start
    
    # 统计结果
    success = sum(1 for r in results if not isinstance(r, Exception))
    errors = len(results) - success
    
    print(f"完成 3000 个请求耗时: {elapsed:.2f}s")
    print(f"成功: {success}, 失败: {errors}")
    print(f"QPS: {3000/elapsed:.2f}")
```

预期结果：
- 耗时: 1-3 秒
- 成功率: > 99%
- QPS: 1000-3000
