# Preference API 使用说明

## 概述

Preference 服务已从项目中移除并独立部署为 `quant/final` 项目中的一个微服务模块。该服务替代了原来的 `PreferenceManager`（SQLite 本地存储 + 内存缓存），通过 REST API 对外提供行业认知读取/追加、结构化偏好管理、LLM 重写和建议应用功能。

**基础 URL：** `http://<server>:<port>/api/v1/preferences`

---

## 一、认证

所有请求需携带 API Key（与 KB API 其他模块一致）：

```
Header: X-API-Key: <your-api-key>
```

来自 `quant/final` 服务端 `.env` 中 `api_key` 的配置值。

---

## 二、API 端点

### 2.1 读取行业认知

获取指定行业/板块的当前偏好认知文本。

```
GET /api/v1/preferences/{sector}/cognition
```

**路径参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `sector` | string | 行业/板块名称，如 `"白酒"`、`"新能源"` |

**响应 `200`：**

```json
{
    "sector": "白酒",
    "text": "茅台批价在 2600 元附近企稳，渠道库存处于低位，终端动销环比改善。五粮液控量挺价策略效果显著，批价回升至 950 以上。",
    "append_count": 2
}
```

**错误 `404`：** 该行业暂无认知数据（从未创建过）。

---

### 2.2 追加行业认知

增量追加一段认知文本到指定行业。当 `append_count` 达到阈值（默认 5）时，服务端自动调用 LLM 将碎片化笔记整合为连贯的认知摘要。

```
POST /api/v1/preferences/{sector}/cognition
```

**路径参数：** 同 `GET`

**请求体：**

```json
{
    "text": "茅台批价企稳回升至 2650，国庆动销超预期。"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `text` | string | 是 | 追加的认知文本段落 |

**响应 `200`：**

```json
{
    "sector": "白酒",
    "status": "appended"
}
```

`status` 取值：
- `"appended"` — 已追加（未触发 LLM 重写）
- `"rewritten"` — 追加后达到阈值，LLM 已完成认知整合重写

**注意事项：**

1. 追加的文本会以 `\n\n---\n\n` 分隔拼接到已有的 `cognition_text` 之后
2. 每次追加后 `append_count` 自增 1
3. 当 `append_count >= 5`（默认阈值），服务端自动调用 LLM 整合所有碎片文本，替换 `cognition_text`，并将 `append_count` 重置为 0
4. LLM 调用失败时不丢数据：保留原始文本，下次追加到阈值后重新尝试重写

---

### 2.3 读取结构化偏好

获取完整的结构化偏好设置及所有行业的认知汇总。

```
GET /api/v1/preferences/structured
```

**无参数。** 首次调用时自动初始化默认值。

**响应 `200`：**

```json
{
    "id": "a1b2c3d4-...",
    "asset_preferences": {
        "sector_weights": {
            "白酒": 0.25,
            "新能源": 0.20
        },
        "avoid_list": ["房地产"],
        "market_cap_preference": "large_cap",
        "whitelist": ["600519.SH", "300750.SZ"]
    },
    "risk_preferences": {
        "position_limits": {
            "单票上限": 0.15
        },
        "max_drawdown_pct": 20.0,
        "stop_loss_pct": 10.0,
        "take_profit_pct": 30.0
    },
    "analysis_preferences": {
        "time_horizon": "medium",
        "depth": "standard",
        "focus_points": ["资金流向", "北向资金"]
    },
    "learned_rules": [
        "白酒板块财报密集期避免重仓",
        "长假前降低仓位至 50% 以下"
    ],
    "industry_cognition": {
        "白酒": "茅台批价企稳...",
        "新能源": "光伏组件价格持续下行..."
    },
    "industry_append_count": {
        "白酒": 2,
        "新能源": 4
    },
    "created_at": "2026-06-01T10:00:00+08:00",
    "updated_at": "2026-06-01T12:00:00+08:00"
}
```

**字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `asset_preferences.sector_weights` | `{sector: weight}` | 各板块投资权重 |
| `asset_preferences.avoid_list` | `[sector, ...]` | 回避板块列表 |
| `asset_preferences.market_cap_preference` | string | 市值偏好：`"large_cap"` / `"mid_cap"` / `"small_cap"` / `"any"` |
| `asset_preferences.whitelist` | `[code, ...]` | 票池白名单（股票代码） |
| `risk_preferences.position_limits` | `{name: value}` | 仓位限制项（可自定义 key） |
| `risk_preferences.max_drawdown_pct` | float | 最大回撤容忍（%） |
| `risk_preferences.stop_loss_pct` | float | 默认止损线（%） |
| `risk_preferences.take_profit_pct` | float | 默认止盈线（%） |
| `analysis_preferences.time_horizon` | string | 分析时间维度 |
| `analysis_preferences.depth` | string | 分析深度 |
| `analysis_preferences.focus_points` | `[string, ...]` | 额外关注点 |
| `learned_rules` | `[string, ...]` | 复盘学到的交易规则 |
| `industry_cognition` | `{sector: text}` | 各行业认知文本汇总 |
| `industry_append_count` | `{sector: int}` | 各行业追加计数（0 表示刚重写过） |

---

### 2.4 更新结构化偏好

更新资产/风控/分析偏好或学到规则。**支持部分更新**——只传需要变更的字段即可。

```
PUT /api/v1/preferences/structured
```

**请求体（部分更新示例，仅修改 risk_preferences）：**

```json
{
    "risk_preferences": {
        "position_limits": {
            "单票上限": 0.12,
            "单行业上限": 0.30
        },
        "max_drawdown_pct": 18.0,
        "stop_loss_pct": 7.0,
        "take_profit_pct": 25.0
    }
}
```

**请求体（完整更新示例）：**

```json
{
    "asset_preferences": {
        "sector_weights": {"白酒": 0.20},
        "avoid_list": [],
        "market_cap_preference": "any",
        "whitelist": []
    },
    "risk_preferences": {
        "position_limits": {},
        "max_drawdown_pct": 20.0,
        "stop_loss_pct": 10.0,
        "take_profit_pct": 30.0
    },
    "analysis_preferences": {
        "time_horizon": "medium",
        "depth": "standard",
        "focus_points": []
    },
    "learned_rules": []
}
```

所有顶层字段均可选。`null` / 不传的字段维持原值不变。

**响应 `200`：** 返回更新后的完整 `StructuredPreferencesResponse`（格式同 2.3 节 GET 响应）。

**注意事项：** 此接口只修改结构化偏好部分（四个 JSONB 字段），不影响行业认知数据。行业认知只能通过 `POST /{sector}/cognition` 追加。

---

### 2.5 应用 LLM 建议

应用复盘 Agent 输出的偏好变更建议——灵活调整板块权重、风控参数、关注点和交易规则。

```
POST /api/v1/preferences/suggestions
```

**请求体：**

```json
{
    "weight_changes": [
        {
            "sector": "白酒",
            "new_weight": 0.30,
            "reason": "白酒板块业绩确定性增强，建议上调至 30%"
        }
    ],
    "risk_param_changes": [
        {
            "param_name": "max_drawdown_pct",
            "new_value": 15.0,
            "reason": "近期波动加剧，收紧最大回撤容忍"
        }
    ],
    "focus_points": [
        {
            "action": "add",
            "point": "北向资金流向"
        },
        {
            "action": "remove",
            "point": "技术面指标"
        }
    ],
    "learned_rules_to_add": [
        "白酒财报密集期（4月/10月）避免重仓",
        "长假前 3 个交易日降低仓位至 50%"
    ]
}
```

**字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `weight_changes` | array | 板块权重变更列表 |
| `weight_changes[].sector` | string | 板块名称 |
| `weight_changes[].new_weight` | float | 新权重值 |
| `weight_changes[].reason` | string? | 变更理由（可选） |
| `risk_param_changes` | array | 风控参数变更列表 |
| `risk_param_changes[].param_name` | string | 参数名（如 `max_drawdown_pct`） |
| `risk_param_changes[].new_value` | float | 新参数值 |
| `risk_param_changes[].reason` | string? | 变更理由（可选） |
| `focus_points` | array | 关注点增删 |
| `focus_points[].action` | string | `"add"` 添加 / `"remove"` 移除 |
| `focus_points[].point` | string | 关注点文本 |
| `learned_rules_to_add` | `[string, ...]` | 要追加的学习规则（自动去重） |

**响应 `200`：**

```json
{
    "status": "applied",
    "applied_changes": {
        "weight_changes": ["白酒"],
        "risk_param_changes": ["max_drawdown_pct"],
        "focus_points": ["北向资金流向", "技术面指标"],
        "learned_rules_added": [
            "白酒财报密集期（4月/10月）避免重仓"
        ]
    }
}
```

`learned_rules_to_add` 中已存在的规则会被静默跳过（不去重追加），`applied_changes.learned_rules_added` 只列出实际新增的规则。

---

## 三、LLM 重写机制

### 触发条件

当对某行业的 `append_industry_cognition` 累计调用次数达到阈值（`preference_rewrite_threshold`，默认值 **5**）时，服务端自动执行 LLM 重写。

### 重写流程

```
1. Append 文本 → cognition_text + "\n\n---\n\n" + new_text
2. append_count += 1
3. 若 append_count >= threshold (5):
   a. 调用 LLM：整合所有碎片化笔记 → 连贯的行业认知摘要
   b. 替换 cognition_text 为整合结果
   c. 重置 append_count = 0
   d. 返回 status: "rewritten"
4. 否则返回 status: "appended"
```

### 故障处理

LLM 调用失败时不会丢失数据：原始碎片文本完整保留，`append_count` 不被重置，下次追加到阈值后自动重新尝试。异常会记录到服务端日志。

---

## 四、客户端使用

### 方式 A：Python SDK（推荐）

`quant/final` 项目提供了 Python 客户端 SDK，与 KB API 其他模块共享同一个 `QuantClient` 入口：

```python
from kbquant.client import QuantClient

async with QuantClient("http://localhost:8000", api_key="your-key") as client:
    # 1. 读取行业认知
    cog = await client.preferences.get_industry_cognition("白酒")
    print(cog.text, cog.append_count)

    # 2. 追加行业认知
    result = await client.preferences.append_industry_cognition(
        "白酒", "茅台批价企稳回升至 2650..."
    )
    print(result.status)  # "appended" | "rewritten"

    # 3. 读取完整结构化偏好
    prefs = await client.preferences.get_structured()
    print(prefs.asset_preferences.sector_weights)

    # 4. 更新结构化偏好（部分更新）
    from kbquant.schemas.preference import StructuredPreferencesUpdate, RiskPreferences
    updated = await client.preferences.update_structured(
        StructuredPreferencesUpdate(
            risk_preferences=RiskPreferences(max_drawdown_pct=15.0)
        )
    )

    # 5. 应用 LLM 建议
    from kbquant.schemas.preference import (
        SuggestionsPayload,
        SuggestionWeightChange,
        SuggestionRiskParam,
    )
    result = await client.preferences.apply_suggestions(
        SuggestionsPayload(
            weight_changes=[
                SuggestionWeightChange(sector="白酒", new_weight=0.30)
            ],
            risk_param_changes=[
                SuggestionRiskParam(param_name="max_drawdown_pct", new_value=15.0)
            ],
            learned_rules_to_add=["长假前降低仓位"],
        )
    )
    print(result.applied_changes)
```

不依赖 SDK 也可直接用 `httpx` 发起原始 HTTP 请求。

### 方式 B：原始 HTTP

```python
import httpx

async with httpx.AsyncClient(base_url="http://localhost:8000") as http:
    headers = {"X-API-Key": "your-key"}

    # 读取行业认知
    r = await http.get("/api/v1/preferences/白酒/cognition", headers=headers)
    r.raise_for_status()
    data = r.json()  # {sector, text, append_count}

    # 追加行业认知
    r = await http.post(
        "/api/v1/preferences/白酒/cognition",
        json={"text": "新增认知段落..."},
        headers=headers,
    )
    data = r.json()  # {sector, status}

    # 读取结构化偏好
    r = await http.get("/api/v1/preferences/structured", headers=headers)
    data = r.json()  # 完整 StructuredPreferencesResponse

    # 更新结构化偏好
    r = await http.put(
        "/api/v1/preferences/structured",
        json={"risk_preferences": {"max_drawdown_pct": 15.0}},
        headers=headers,
    )

    # 应用 LLM 建议
    r = await http.post(
        "/api/v1/preferences/suggestions",
        json={
            "weight_changes": [{"sector": "白酒", "new_weight": 0.30}],
            "learned_rules_to_add": ["规则文本"],
        },
        headers=headers,
    )
```

---

## 五、错误码

| 状态码 | 说明 |
|--------|------|
| 200 | 成功 |
| 401 | API Key 无效或缺失 |
| 404 | 请求的行业 `sector` 不存在（GET cognition 时） |
| 500 | 服务端内部错误 |

---

## 六、与旧 PreferenceManager 的映射

| 旧调用 | 新 API |
|--------|--------|
| `prefs.get_industry_cognition(sector)` | `GET /preferences/{sector}/cognition` |
| `prefs.append_industry_cognition(sector, text)` | `POST /preferences/{sector}/cognition` |
| `prefs.get()` (全部偏好) | `GET /preferences/structured` |
| `prefs.get_section(key)` | `GET /preferences/structured` → 取对应字段 |
| `prefs.update_section(key, value)` | `PUT /preferences/structured` (传单个字段) |
| `prefs.apply_llm_suggestions(suggestions)` | `POST /preferences/suggestions` |
| `prefs.check_and_rewrite(sector, llm_client)` | **自动触发**：`POST /{sector}/cognition` 达阈值时服务端执行 |
| `prefs._llm_rewrite_cognition(sector, text, llm_client)` | **不再暴露**：服务端内部执行 |

---

## 七、配置参数

服务端 `.env` / `Settings` 中可调整的配置项：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `preference_rewrite_threshold` | `5` | 行业认知追加多少次后触发 LLM 自动重写 |

客户端无需关心重写逻辑——阈值判断和 LLM 调用均在服务端完成。
