# Preference 模块移除 & 接口化迁移文档

## 概述

当前项目中 preference（偏好）的存储、读写、缓存、LLM 重写等逻辑全部内嵌在项目内部。本文档梳理所有 preference 相关代码，作为将其移除并改为外部接口调用的迁移依据。

---

## 一、Preference 数据模型

### 1.1 数据结构（默认值定义于 `src/preferences/manager.py:15-38`）

```python
_DEFAULT_PREFERENCES = {
    "asset_preferences": {
        "关注板块权重": {},       # {sector: weight}
        "回避板块": [],            # [sector, ...]
        "偏好市值": None,
        "票池白名单": [],
    },
    "risk_preferences": {
        "max_position_pct": 0.10,
        "max_sector_concentration": 0.30,
        "max_open_positions": 5,
        "max_drawdown": 0.15,
        "stop_loss_default": 0.05,
        "take_profit_default": 0.10,
    },
    "analysis_preferences": {
        "preferred_time_horizon": "medium_term",
        "analysis_depth": "standard",
        "extra_focus_points": [],
    },
    "learned_rules": [],
    "industry_cognition": {},        # {sector: "认知文本..."}
    "industry_append_count": {},     # {sector: int}
}
```

### 1.2 数据库表（`src/models/tables.py:46-53`）

- 表名：`preferences`
- 单行设计：`id = "default"`，`data` 列存 JSON 文本
- 由 `src/db.py:29-31` 的 `init_db()` 通过 SQLAlchemy auto-discovery 自动创建

---

## 二、核心模块：PreferenceManager

**文件：** `src/preferences/manager.py`（完整文件，194 行）

| 方法 | 功能 | 读写类型 |
|------|------|----------|
| `__init__()` | 初始化内存缓存 `_data = None` | - |
| `_get_row(db)` | 从 SQLite 读取/创建 PreferenceRow | **读+写** |
| `load()` | 启动时从 DB 加载到内存 | **读** |
| `get()` | 返回全部偏好 dict | **读** |
| `get_section(key)` | 返回某个 section | **读** |
| `update_section(key, value)` | 更新某个 section 并持久化 | **写** |
| `_persist(data)` | 将内存 dict 写入 SQLite | **写** |
| `get_industry_cognition(sector)` | 获取行业认知文本 | **读** |
| `append_industry_cognition(sector, text)` | 追加行业认知+计数器递增 | **写** |
| `check_and_rewrite(sector, llm_client)` | 计数器达阈值时触发 LLM 重写 | **写**（含 LLM 调用） |
| `_llm_rewrite_cognition(sector, full_text, llm_client)` | 调用 LLM 整合碎片化笔记 | **LLM 调用** |
| `apply_llm_suggestions(suggestions)` | 应用复盘建议（权重/风控参数/关注点） | **写** |

**包文件：** `src/preferences/__init__.py`（空文件）

---

## 三、Preference 的读写入口（对外暴露的 API）

Preference 通过两个 LLM-facing tool 暴露给 Agent：

### 3.1 读取：`get_preferences` 工具

**文件：** `src/tools/knowledge.py:149-165`

```
工具名: get_preferences
参数: sector: str (行业/板块名称)
功能: 获取指定行业的当前投资偏好认知文本
分类: knowledge
调用链: get_preferences() → prefs.get_industry_cognition(sector)
```

**Args Schema：** `GetPreferencesArgs`（`src/tools/knowledge.py:39-40`）

### 3.2 写入：`append_preference` 工具

**文件：** `src/tools/writer.py:406-420`

```
工具名: append_preference
参数: sector: str, text: str
功能: 增量追加行业偏好认知文本
分类: writer
调用链: append_preference() → prefs.append_industry_cognition(sector, text)
```

**Args Schema：** `AppendPreferenceArgs`（`src/tools/writer.py:131-133`）

---

## 四、依赖注入链路（prefs 如何传递到工具层）

```
main.py (实例化)
  ├─ self.prefs = PreferenceManager()                         # line 84
  ├─ await self.prefs.load()                                  # line 102
  └─ PipelineOrchestrator(quant, market, prefs, ...)          # line 88
       │
       └─ orchestrator.py (持有 + 注入)
            ├─ self.prefs = prefs                              # line 55
            └─ init_ctx(quant=..., market=..., prefs=self.prefs, ...)  # lines 80, 262, 315, 343, 411, 442
                 │
                 └─ tools/context.py (contextvar)
                      ├─ ToolContext.prefs: PreferenceManager   # line 22
                      └─ get_ctx().prefs → 工具层访问           # line 51
```

---

## 五、Agent 中对 Preference 的使用

### 5.1 深度分析 Agent（读取偏好）

**文件：** `src/agents/deep_analysis.py`

| 位置 | 内容 |
|------|------|
| line 14 | SYS_OVERALL prompt：提到"行业偏好" |
| line 25 | SYS_RESEARCH prompt：提到"拉取相关行业的偏好认知文本" |
| line 68 | Stage 1 工具列表：包含 `"get_preferences"` |

### 5.2 复盘 Agent（写入偏好）

**文件：** `src/agents/reflection.py`

| 位置 | 内容 |
|------|------|
| line 21 | SYS_OVERALL prompt：提到"更新行业偏好" |
| line 49 | SYS_WRITE prompt：提到 `append_preference` |
| line 140 | `self._write_tools`：包含 `"append_preference"` |

---

## 六、配置项

**文件：** `src/config.py:99`

```python
preference_rewrite_threshold: int = 5  # 行业认知追加多少次后触发 LLM 全量重写
```

此配置仅在 `PreferenceManager.check_and_rewrite()` 中使用。

---

## 七、完整文件清单

### 7.1 需要删除的文件

| 文件 | 说明 |
|------|------|
| `src/preferences/manager.py` | PreferenceManager 完整实现 |
| `src/preferences/__init__.py` | 空包 init |

### 7.2 需要修改的文件

| 文件 | 行号 | 修改内容 |
|------|------|----------|
| **`src/models/tables.py`** | 46-53 | 删除 `PreferenceRow` 类 |
| **`src/config.py`** | 99 | 删除 `preference_rewrite_threshold` |
| **`src/tools/context.py`** | 11, 22, 35 | 删除 `PreferenceManager` 导入、字段、参数 |
| **`src/tools/knowledge.py`** | 39-40, 149-165 | 删除 `GetPreferencesArgs` 和 `get_preferences` 工具 |
| **`src/tools/writer.py`** | 131-133, 370, 406-420 | 删除 `AppendPreferenceArgs`、`create_feedback` 描述中的引用、`append_preference` 工具 |
| **`src/tools/__init__.py`** | 18 | 删除 `init_tool_deps` 的 `prefs` 参数 |
| **`src/agents/deep_analysis.py`** | 14, 25, 68 | 删除 prompt 中"行业偏好"引用、从 Stage 1 工具列表移除 `get_preferences` |
| **`src/agents/reflection.py`** | 21, 49, 140 | 删除 prompt 中"行业偏好"引用、从 `_write_tools` 移除 `append_preference` |
| **`src/main.py`** | 27, 84, 88, 102 | 删除导入、实例化、传参、`load()` 调用 |
| **`src/pipeline/orchestrator.py`** | 26, 44, 55, 80, 262, 315, 343, 411, 442 | 删除导入、构造函数参数、字段、所有 `init_ctx(prefs=...)` 中的 `prefs` |
| **`tests/test_tools.py`** | 138-139, 153, 167, 189, 217 | 删除 mock、导入、测试用例 |
| **`tests/test_agent_deep_analysis.py`** | 60-61, 64 | 删除 mock、`init_ctx` 中的 `prefs` |
| **`scripts/run_deep_analysis_smoke.py`** | 240-245, 285-289 | 删除 `FakePrefs` 类及使用 |
| **`scripts/run_reflection_smoke.py`** | 21, 291-297, 431-435 | 删除 `FakePrefs` 导入、模拟调用、`init_ctx` 中的 `prefs` |
| **`README.md`** | 66, 103, 167, 231-238 | 删除 preference 相关文档 |

---

## 八、接口化方案建议

将 preference 的存储和修改移到外部服务后，需要定义以下接口：

### 8.1 读取接口

```
GET /api/v1/preferences/{sector}/cognition
Response: { "sector": "白酒", "text": "认知文本..." }
```

替代 `get_industry_cognition(sector)` → `get_preferences` 工具。

### 8.2 写入接口

```
POST /api/v1/preferences/{sector}/cognition
Body: { "text": "新增认知段落" }
Response: { "sector": "白酒", "status": "appended" }
```

替代 `append_industry_cognition(sector, text)` → `append_preference` 工具。

### 8.3 结构偏好接口（可选，按需）

```
GET  /api/v1/preferences/structured    → 获取 asset_preferences / risk_preferences / analysis_preferences
PUT  /api/v1/preferences/structured    → 更新结构化偏好
POST /api/v1/preferences/suggestions   → 应用 LLM 建议的偏好变更
```

替代 `get()` / `get_section()` / `update_section()` / `apply_llm_suggestions()`。

### 8.4 LLM 重写逻辑

`check_and_rewrite()` 和 `_llm_rewrite_cognition()` 中的 LLM 重写逻辑应移到外部服务中实现（追加次数达到阈值后由服务端自动触发重写），客户端无需关心。   

### 8.5 ToolContext 变更

删除 `prefs` 字段后，`get_preferences` 和 `append_preference` 工具改为直接调用外部 HTTP API（通过 `get_ctx().quant` 或新增的 prefs client）。

---

## 九、数据迁移

现有 SQLite 中 `preferences` 表的数据需要导出并导入到新的外部服务。导出方式：

```sql
SELECT data FROM preferences WHERE id = 'default';
```

将 JSON 数据按 section 拆分为外部服务所需的格式后导入。

---

## 十、数据流图

```
                    ┌── 当前架构 ──┐
                    │              │
    Agent tools ──→ ToolContext.prefs ──→ PreferenceManager ──→ SQLite
    (get/append)       (contextvar)         (内存缓存)         (preferences表)
                                             │
                                        config.py
                                  (rewrite_threshold)


                    ┌── 目标架构 ──┐
                    │              │
    Agent tools ──→ HTTP Client ──→ 外部 Preference API
    (get/append)    (quant或新client)    │
                                    ┌────┴────┐
                                    │ 存储层   │
                                    │ LLM重写  │
                                    │ 计数器   │
                                    └─────────┘
```
