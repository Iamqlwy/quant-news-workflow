# final/ KB 新增/修改 API 需求

本文档列出 workflow 项目所需要的、final/ KB 当前缺失或需要修改的 API。

---

## 1. PUT /trading/{trade_id} — 更新交易状态

**用途**：风控 Agent 通过/拒绝交易后，更新交易记录的状态。

**当前状态**：`POST /trading/` 创建时默认 `status = "pending"`，但无更新接口。

**请求**：
```json
{
  "status": "approved" | "rejected",
  "reason": "风控拒绝原因（rejected 时必填）"
}
```

**响应**：`TradingOperationResponse`（更新后的完整交易记录）

**备注**：如果 `approved`，可能还需要更新 `executed_at` 字段。

---

## 2. 宏观报告 — 新增模型 + API

**用途**：宏观 Agent（紧急/日更）读写宏观形势报告。

### 2.1 数据模型

建议新增表 `macro_reports`：

| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| version | int | 版本号，每次更新递增 |
| content | text | 报告正文（Markdown，五段式结构） |
| summary | text | 一句话摘要，供下游 Agent 快速读取 |
| changed_sections | list[str] | 本次更新了哪些章节 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

### 2.2 GET /macro-report/current

获取当前最新版本的宏观报告。

响应：
```json
{
  "id": "uuid",
  "version": 5,
  "content": "# 当前宏观定位\n...",
  "summary": "货币宽松确立，信用仍弱，建议超配科技、低配地产",
  "changed_sections": ["当前宏观定位", "行业配置建议"],
  "created_at": "...",
  "updated_at": "..."
}
```

### 2.3 PUT /macro-report

更新宏观报告（增量修改）。传入新的 content，服务端自动递增 version，旧版本保留。

请求：
```json
{
  "content": "# 更新后的完整报告...",
  "summary": "一句话摘要",
  "changed_sections": ["资产观点", "行业配置建议"]
}
```

响应：同上 `GET /macro-report/current`。

### 2.4 GET /macro-report/history

获取宏观报告的所有历史版本（按 version 倒序）。

响应：
```json
{
  "items": [
    { "id": "...", "version": 5, "summary": "...", "changed_sections": [...], "updated_at": "..." },
    { "id": "...", "version": 4, "summary": "...", "changed_sections": [...], "updated_at": "..." }
  ]
}
```

---

## 3. GET /information/ — 增加日期范围筛选

**用途**：复盘 Agent 的 `get_news_during_period` 工具需要按时间范围查询资讯。

**当前状态**：`GET /information/` 支持 `info_type`、`source`、`status` 三个可选筛选参数。

**需要新增参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `from_date` | string (ISO date) | 资讯发布时间起始 |
| `to_date` | string (ISO date) | 资讯发布时间截止 |
| `entity` | string | 按实体名称模糊搜索 |
| `ticker` | string | 按股票代码搜索 |



---

## 总结

| # | 变更 | 类型 | 优先级 |
|---|------|------|--------|
| 1 | `PUT /trading/{trade_id}` | 新增端点 | 高（风控 Agent 依赖） |
| 2 | `macro_reports` 表 + 3 个端点 | 新增模型+API | 高（宏观 Agent 依赖） |
| 3 | `GET /information/` 加日期筛选 | 修改现有端点 | 中（复盘 Agent 依赖） |
