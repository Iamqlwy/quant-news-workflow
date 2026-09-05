# 量化交易知识库 API 接口文档

> **Base URL**: `http://{host}:{port}/api/v1`
> **认证方式**: 所有接口需要在请求头中携带 `X-API-Key`
> **版本**: 0.1.0

---

## 目录

1. [系统](#1-系统)
2. [Python 客户端 SDK 快速开始](#2-python-客户端-sdk-快速开始)
3. [资讯管理（Information）](#3-资讯管理information)
4. [实体管理（Entities）](#4-实体管理entities)
5. [世界节点（Nodes）](#5-世界节点nodes)
6. [分析记录（Analysis）](#6-分析记录analysis)
7. [交易操作（Trading）](#7-交易操作trading)
8. [反馈复盘（Feedback）](#8-反馈复盘feedback)
9. [搜索（Search）](#9-搜索search)
10. [处理流水线（Pipeline）](#10-处理流水线pipeline)
11. [时效管理（Validity）](#11-时效管理validity)
12. [冲突检测（Conflicts）](#12-冲突检测conflicts)
13. [重要性排序（Ranking）](#13-重要性排序ranking)
14. [证据追溯（Evidence）](#14-证据追溯evidence)
15. [时序查询（Queries）](#15-时序查询queries)
16. [宏观报告（Macro Report）](#16-宏观报告macro-report)

---

## 1. 系统

### `GET /health`

健康检查，不需要 API Key。

**响应示例**：
```json
{
  "status": "ok",
  "db": "connected",
  "version": "0.1.0"
}
```

**客户端调用**：
```python
from src.client import QuantClient

async with QuantClient("http://localhost:8000") as client:
    result = await client.health()
    # result: HealthResponse(status="ok", db="connected", version="0.1.0")
```

---

## 2. Python 客户端 SDK 快速开始

项目提供了 `src/client/` 下的异步 Python SDK，封装了所有 API 调用。

### 初始化

```python
from src.client import QuantClient

# 推荐：async context manager，自动关闭连接
async with QuantClient("http://localhost:8000", api_key="your-key") as client:
    # 通过子模块访问各领域接口
    info = await client.information.ingest(...)
    nodes = await client.nodes.list(...)

# 或手动管理生命周期
client = QuantClient("http://localhost:8000")
await client.health()
await client.close()
```

### 子模块属性一览

| 属性 | 类 | 说明 |
|------|-----|------|
| `client.information` | `InformationClient` | 资讯录入、查询、去重、合并、实体提取 |
| `client.entities` | `EntityClient` | 实体CRUD、关系管理、影响路径 |
| `client.nodes` | `NodeClient` | 节点CRUD、状态版本、附件挂载、压缩 |
| `client.analysis` | `AnalysisClient` | 分析记录创建与查询 |
| `client.trading` | `TradingClient` | 交易操作记录与查询 |
| `client.feedback` | `FeedbackClient` | 反馈复盘、经验教训检索 |
| `client.search` | `SearchClient` | 混合搜索、任务搜索、多粒度搜索、相似案例 |
| `client.pipeline` | `PipelineClient` | 处理队列管理、状态更新、统计 |
| `client.validity` | `ValidityClient` | 时效记录管理、过期/延期、时效检查 |
| `client.conflicts` | `ConflictClient` | 冲突检测、查询、解决 |
| `client.ranking` | `RankingClient` | 重要性评分计算、排名查询 |
| `client.evidence` | `EvidenceClient` | 证据链追溯 |
| `client.queries` | `QueriesClient` | 时序查询、状态差异对比 |

### 异步遍历（自动翻页）

大部分 list 方法都有对应的 `list_iter` 版本，返回 `AsyncGenerator`，自动翻页遍历全部数据：

```python
async for item in client.information.list_iter(info_type="news"):
    print(item["title"])
```

### 异常类型

| 异常类 | 说明 |
|--------|------|
| `QuantClientError` | 所有客户端异常的基类 |
| `QuantClientHTTPError` | HTTP 4xx/5xx 错误 |
| `QuantClientAuthError` | 认证失败（401） |
| `QuantClientNotFoundError` | 资源不存在（404） |
| `QuantClientConnectionError` | 网络连接失败 |

---

## 3. 资讯管理（Information）

所有接口前缀：`/api/v1/information`
客户端子模块：`client.information`

### 3.1 `POST /` — 录入原始资讯

将一条原始资讯（新闻、研报、社交媒体等）录入系统，作为知识图谱的数据入口。入库后会自动进入处理流水线（去重 → 实体提取 → 节点挂载 → 重要性评分）。

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `title` | string | 是 | 资讯标题/摘要 |
| `body` | string | 是 | 资讯正文 |
| `source` | string | 是 | 来源，如"央行官网"、"Reuters"、"Twitter" |
| `source_url` | string | 否 | 原始URL |
| `published_at` | datetime | 是 | 资讯发布时间（非入库时间） |
| `info_type` | string | 是 | 类型：`news` / `report` / `social_media` / `filing` / `research` / `other` |
| `language` | string | 否 | 语言代码，默认 `"zh"` |
| `raw_metadata` | object | 否 | 扩展元数据 |

**客户端调用**：
```python
from src.schemas.information import RawInformationCreate

data = RawInformationCreate(
    title="央行降准0.5个百分点",
    body="中国人民银行决定于2024年1月25日下调金融机构存款准备金率0.5个百分点...",
    source="央行官网",
    source_url="http://www.pbc.gov.cn/...",
    published_at=datetime(2024, 1, 24, 17, 0, 0),
    info_type="news",
    language="zh",
)
result = await client.information.ingest(data)
# result: RawInformationResponse (含 id, processing_status, importance_score 等)
```

---

### 3.2 `GET /` — 分页查询资讯列表

支持多维度筛选和模糊搜索。

**查询参数**：
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `page` | int | 否 | 页码，默认 1 |
| `page_size` | int | 否 | 每页条数，1~100，默认 20 |
| `info_type` | string | 否 | 按资讯类型过滤 |
| `source` | string | 否 | 按来源过滤 |
| `status` | string | 否 | 按处理状态过滤 |
| `from_date` | string | 否 | 发布时间起始（ISO date） |
| `to_date` | string | 否 | 发布时间截止（ISO date） |
| `entity` | string | 否 | 按实体名称模糊搜索 |
| `ticker` | string | 否 | 按股票代码搜索 |

**客户端调用**：
```python
# 分页查询
result = await client.information.list(page=1, page_size=20, info_type="news", source="央行官网")
# result: PaginatedResponse(items=[...], total=100, page=1, page_size=20)

# 遍历全部（自动翻页）
async for item in client.information.list_iter(info_type="news", page_size=100):
    print(item["title"])
```

---

### 3.3 `GET /{info_id}` — 获取单条资讯详情

返回指定 ID 的资讯完整信息，包括处理状态和重要性评分。

**客户端调用**：
```python
info = await client.information.get("uuid-xxxx")
# info: RawInformationResponse
```

---

### 3.4 `POST /check-duplicate` — 去重检查

在录入新资讯前，检查是否与已有资讯重复。通过向量相似度（embedding）计算标题+正文的语义相似度。

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `title` | string | 是 | 待检查的资讯标题 |
| `body` | string | 是 | 待检查的资讯正文 |
| `threshold` | float | 否 | 相似度阈值，默认 0.85，超过即视为重复 |

**响应**：返回是否重复、最匹配的已有资讯ID及相似度分数。

**客户端调用**：
```python
from src.schemas.information import DedupCheckRequest

result = await client.information.check_duplicate(DedupCheckRequest(
    title="央行降准0.5个百分点",
    body="中国人民银行决定于...",
    threshold=0.85,
))
# result: DedupCheckResponse(is_duplicate=True, primary_id=UUID(...), similarity_score=0.96, matches=[...])
```

---

### 3.5 `POST /merge` — 合并重复资讯

将两条重复资讯合并，保留一条作为主版本，另一条标记为重复并记录去重类型。

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `primary_id` | UUID | 是 | 主资讯ID（保留版本） |
| `duplicate_id` | UUID | 是 | 重复资讯ID（被合并版本） |
| `dedup_type` | string | 是 | 去重类型：`exact_duplicate` / `reprint` / `follow_up` / `same_event` / `superseded` |
| `dedup_rationale` | string | 否 | 去重理由 |

**客户端调用**：
```python
from src.schemas.information import InformationMergeRequest

result = await client.information.merge(InformationMergeRequest(
    primary_id=UUID("..."),
    duplicate_id=UUID("..."),
    dedup_type="same_event",
    dedup_rationale="两篇报道描述同一事件",
))
# result: InformationMergeResponse
```

---

### 3.6 `POST /{info_id}/extract-entities` — 提取实体

对一条已入库的资讯执行实体提取，识别其中提到的公司、人物、板块、产品等命名实体，并记录实体在资讯中的角色（主语/宾语/背景/提及）和置信度。

**请求体**：包含 `entities` 数组，每项含 `name`、`entity_type`、`role`、`relevance_score`、`extraction_confidence`。

**客户端调用**：
```python
from src.schemas.entity import EntityExtractRequest

entities = await client.information.extract_entities("uuid-xxxx", EntityExtractRequest(
    entities=[
        {"name": "贵州茅台", "entity_type": "company", "role": "subject", "relevance_score": 0.95, "extraction_confidence": 0.92},
        {"name": "白酒板块", "entity_type": "sector", "role": "context", "relevance_score": 0.8, "extraction_confidence": 0.85},
    ]
))
# result: list[dict] — 提取到的实体列表
```

---

### 3.7 `GET /{info_id}/entities` — 查看资讯关联的实体

返回某条资讯已提取到的所有实体及其在资讯中的角色。

**客户端调用**：
```python
entities = await client.information.get_entities("uuid-xxxx")
# result: list[dict] — 关联的实体列表
```

---

## 4. 实体管理（Entities）

所有接口前缀：`/api/v1/entities`
客户端子模块：`client.entities`

实体是从资讯中提取出的命名实体（公司、股票代码、板块、概念、政策等），是连接资讯与世界节点的桥梁。

### 4.1 `POST /` — 创建实体

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 实体名称 |
| `entity_type` | string | 是 | 类型：`company` / `stock_code` / `sector` / `concept` / `product` / `policy` / `institution` / `region` / `person` / `upstream` / `downstream` |
| `aliases` | list | 否 | 别名列表 |
| `metadata_` | object | 否 | 提取上下文信息 |
| `linked_node_id` | UUID | 否 | 关联的世界节点ID |

**客户端调用**：
```python
from src.schemas.entity import EntityCreate

entity = await client.entities.create(EntityCreate(
    name="贵州茅台",
    entity_type="company",
    aliases=["茅台", "600519.SH"],
    linked_node_id=UUID("..."),
))
# entity: EntityResponse
```

---

### 4.2 `GET /` — 分页查询实体列表

**查询参数**：`entity_type`（按类型过滤）、`search`（名称模糊搜索）、`page`、`page_size`

**客户端调用**：
```python
# 分页查询
result = await client.entities.list(entity_type="company", search="茅台", page=1, page_size=20)

# 遍历全部
async for item in client.entities.list_iter(entity_type="company"):
    print(item["name"])
```

---

### 4.3 `POST /relationships` — 创建/更新实体关系

记录两个实体之间的关系（如供应链上下游、竞争、控股等），需附带支持该关系的证据资讯ID。

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `source_entity_id` | UUID | 是 | 源实体 |
| `target_entity_id` | UUID | 是 | 目标实体 |
| `relationship_type` | string | 是 | 关系类型 |
| `strength` | float | 否 | 关系强度 0.0~1.0 |
| `evidence_info_ids` | list[UUID] | 否 | 支持该关系的证据资讯ID |
| `description` | string | 否 | 关系说明 |

**客户端调用**：
```python
from src.schemas.entity import EntityRelationshipCreate

rel = await client.entities.create_relationship(EntityRelationshipCreate(
    source_entity_id=UUID("..."),
    target_entity_id=UUID("..."),
    relationship_type="supplier_of",
    strength=0.85,
    evidence_info_ids=[UUID("...")],
    description="茅台为白酒板块龙头企业",
))
# rel: EntityRelationshipResponse
```

---

### 4.4 `GET /{entity_id}/relationships` — 查看实体的所有关系

返回该实体作为源或目标的所有关系。

**客户端调用**：
```python
rels = await client.entities.get_relationships("uuid-xxxx")
# rels: list[EntityRelationshipResponse]
```

---

### 4.5 `GET /impact-path/{entity_id}` — 影响路径分析

从指定实体出发，沿关系图谱找出影响传播路径。用于分析"某事件对某实体影响将通过什么路径传导"。

**查询参数**：
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `depth` | int | 3 (1~5) | 搜索深度 |
| `direction` | string | `"downstream"` | 方向：`downstream`（下游）/ `upstream`（上游）/ `both` |

**客户端调用**：
```python
path = await client.entities.impact_path("uuid-xxxx", depth=3, direction="downstream")
# path: ImpactPathResponse(root=EntityResponse(...), paths=[...])
```

---

## 5. 世界节点（Nodes）

所有接口前缀：`/api/v1/nodes`
客户端子模块：`client.nodes`

世界节点是知识图谱的核心组织单元，代表一个投资标的或分析主题（如"贵州茅台"、"白酒板块"、"货币政策"），支持树形层级结构（Macro → Sector → Company）。

### 5.1 `POST /` — 创建世界节点

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 节点名称 |
| `node_type` | string | 是 | 类型：`company` / `sector` / `macro_theme` / `concept` / `product` / `policy` / `institution` / `region` / `person` |
| `description` | string | 否 | 简要描述 |
| `parent_node_id` | UUID | 否 | 父节点ID，构建树形层级 |
| `ticker` | string | 否 | 股票代码（仅公司节点） |
| `aliases` | list | 否 | 别名列表 |
| `metadata_` | object | 否 | 扩展属性 |

**客户端调用**：
```python
from src.schemas.node import WorldNodeCreate

node = await client.nodes.create(WorldNodeCreate(
    name="贵州茅台",
    node_type="company",
    description="贵州茅台酒股份有限公司",
    ticker="600519.SH",
    aliases=["茅台", "贵州茅台酒"],
    parent_node_id=UUID("..."),
))
# node: WorldNodeResponse
```

---

### 5.2 `GET /` — 分页查询节点列表

**查询参数**：`node_type`（按类型过滤）、`page`、`page_size`

**客户端调用**：
```python
result = await client.nodes.list(node_type="company", page=1, page_size=20)

# 遍历全部
async for item in client.nodes.list_iter(node_type="sector"):
    print(item["name"])
```

---

### 5.3 `GET /{node_id}` — 获取节点详情

**客户端调用**：
```python
node = await client.nodes.get("uuid-xxxx")
# node: WorldNodeResponse
```

---

### 5.4 `POST /{node_id}/attachments` — 挂载资讯/分析到节点

将一条资讯或分析记录挂载到节点上，并标注挂载角色（如"主要证据"、"风险证据"、"背景参考"等），形成节点→证据的追溯链。

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `attachment_type` | string | 是 | 挂载类型：`raw_info` / `analysis` |
| `attachment_id` | UUID | 是 | 挂载对象ID |
| `role` | string | 是 | 角色：`primary` / `secondary` / `background` / `risk` / `historical_reference` / `driver_evidence` / `risk_evidence` |
| `relevance_score` | float | 否 | 相关性评分 0.0~1.0 |

**客户端调用**：
```python
from src.schemas.node import NodeAttachmentCreate

att = await client.nodes.attach("uuid-xxxx", NodeAttachmentCreate(
    attachment_type="raw_info",
    attachment_id=UUID("..."),
    role="driver_evidence",
    relevance_score=0.92,
))
# att: NodeAttachmentResponse
```

---

### 5.5 `GET /{node_id}/attachments` — 查看节点的所有挂载

**查询参数**：`role`（按角色过滤）、`attachment_type`（按挂载类型过滤）

**客户端调用**：
```python
atts = await client.nodes.get_attachments("uuid-xxxx", role="driver_evidence", attachment_type="raw_info")
# atts: list[NodeAttachmentResponse]
```

---

### 5.6 `GET /{node_id}/state/current` — 获取节点当前状态

返回节点的最新状态快照，包含：核心投资逻辑、主要驱动因素、风险列表、关注点、最近变化、不确定性标记、支撑该状态的关键证据ID列表。

**客户端调用**：
```python
state = await client.nodes.get_current_state("uuid-xxxx")
# state: NodeStateResponse (含 core_logic, primary_drivers, risks, focus_points 等)
```

---

### 5.7 `POST /{node_id}/state` — 更新节点状态

更新节点状态会产生一个新版本（version 递增），旧版本自动标记失效时间。用于记录投资逻辑随时间的演变。

**请求体**：`core_logic`、`primary_drivers`、`risks`、`focus_points`、`recent_changes`、`uncertainty_flags`、`key_evidence_ids`、`state_summary`

**客户端调用**：
```python
from src.schemas.node import NodeStateCreate

new_state = await client.nodes.update_state("uuid-xxxx", NodeStateCreate(
    core_logic="受益于春节消费旺季，预计Q1营收增长15%以上",
    primary_drivers=[
        {"driver": "消费旺季", "strength": 0.8, "evidence_ids": [...]},
        {"driver": "提价预期", "strength": 0.6, "evidence_ids": [...]},
    ],
    risks=[{"risk": "政策收紧", "severity": "medium", "evidence_ids": [...]}],
    key_evidence_ids=[UUID("..."), UUID("...")],
))
# new_state: NodeStateResponse (version 递增)
```

---

### 5.8 `GET /{node_id}/state/history` — 查看节点状态变更历史

返回该节点所有历史状态版本，按时间倒序。

**客户端调用**：
```python
history = await client.nodes.get_state_history("uuid-xxxx")
# history: list[NodeStateResponse] — 所有历史版本
```

---

### 5.9 `POST /{node_id}/compress` — 节点摘要压缩

当节点挂载的证据过多时，调用LLM对状态摘要进行压缩，减少上下文长度。压缩后生成新的状态记录。

**请求体**：
| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `force` | bool | `false` | 是否强制压缩 |
| `target_compression_ratio` | float | `0.3` | 目标压缩比 |

**客户端调用**：
```python
from src.schemas.node import NodeCompressionRequest

result = await client.nodes.compress("uuid-xxxx", NodeCompressionRequest(
    force=True,
    target_compression_ratio=0.3,
))
# result: NodeCompressionResponse (含 before/after 证据数、字符数、新状态ID、摘要文本)
```

---

## 6. 分析记录（Analysis）

所有接口前缀：`/api/v1/analysis`
客户端子模块：`client.analysis`

记录对资讯/节点进行的各类分析（影响分析、风险评估、估值分析等），是交易决策的中间产物。

### 6.1 `POST /` — 创建分析记录

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `title` | string | 是 | 分析标题 |
| `content` | string | 是 | 分析正文 |
| `analysis_type` | string | 是 | 类型：`impact_analysis` / `driver_assessment` / `risk_evaluation` / `sentiment` / `valuation` / `technical` / `macro` |
| `agent_id` | string | 否 | 执行分析的Agent标识 |
| `confidence` | float | 否 | 置信度 0.0~1.0 |
| `parent_analysis_id` | UUID | 否 | 父分析ID（链式分析） |
| `root_raw_info_ids` | list[UUID] | 否 | 触发该分析的原始资讯ID |
| `time_horizon` | string | 否 | 时效：`short_term` / `medium_term` / `long_term` |

**客户端调用**：
```python
from src.schemas.analysis import AnalysisCreate

analysis = await client.analysis.create(AnalysisCreate(
    title="降准对白酒板块的影响分析",
    content="本次降准释放流动性约1万亿，对白酒板块形成利好...",
    analysis_type="impact_analysis",
    agent_id="macro_agent_v2",
    confidence=0.85,
    root_raw_info_ids=[UUID("...")],
    time_horizon="short_term",
))
# analysis: AnalysisResponse
```

---

### 6.2 `GET /` — 分页查询分析列表

**查询参数**：`analysis_type`、`agent_id`、`confidence_min`（最低置信度）、`page`、`page_size`

**客户端调用**：
```python
result = await client.analysis.list(analysis_type="impact_analysis", confidence_min=0.7, page=1, page_size=20)

# 遍历全部
async for item in client.analysis.list_iter(agent_id="macro_agent_v2"):
    print(item["title"])
```

---

### 6.3 `GET /{analysis_id}` — 获取分析详情

**客户端调用**：
```python
detail = await client.analysis.get("uuid-xxxx")
# detail: AnalysisResponse
```

---

## 7. 交易操作（Trading）

所有接口前缀：`/api/v1/trading`
客户端子模块：`client.trading`

记录基于分析做出的交易决策和操作，是整个知识驱动交易闭环的"行动"环节。

### 7.1 `POST /` — 创建交易操作记录

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `operation_type` | string | 是 | 操作类型：`buy` / `sell` / `skip` / `track` / `stop_loss` / `take_profit` |
| `target_node_id` | UUID | 否 | 操作标的对应的节点ID |
| `trigger_analysis_id` | UUID | 否 | 触发该操作的分析ID |
| `trigger_raw_ids` | list[UUID] | 否 | 触发该操作的原始证据资讯ID |
| `symbol` | string | 否 | 交易代码 |
| `quantity` | float | 否 | 交易数量 |
| `price` | float | 否 | 成交价格 |
| `rationale` | string | 否 | 操作理由 |
| `expected_impact` | string | 否 | 预期影响 |
| `risk_level` | string | 否 | 风险等级：`low` / `medium` / `high` / `critical` |
| `status` | string | 否 | 状态：`pending` / `executed` / `cancelled` / `expired` |

**客户端调用**：
```python
from src.schemas.trading import TradingOperationCreate

trade = await client.trading.create(TradingOperationCreate(
    operation_type="buy",
    target_node_id=UUID("..."),
    trigger_analysis_id=UUID("..."),
    symbol="600519.SH",
    quantity=1000,
    price=1850.00,
    rationale="降准利好白酒，预计短期上涨5-10%",
    risk_level="medium",
))
# trade: TradingOperationResponse
```

---

### 7.2 `GET /` — 分页查询交易列表

**查询参数**：`operation_type`、`node_id`、`symbol`、`status`、`page`、`page_size`

**客户端调用**：
```python
result = await client.trading.list(operation_type="buy", symbol="600519.SH", status="pending", page=1, page_size=20)

# 遍历全部
async for item in client.trading.list_iter(status="executed"):
    print(item["symbol"], item["operation_type"])
```

---

### 7.3 `GET /{trade_id}` — 获取交易详情

**客户端调用**：
```python
trade = await client.trading.get("uuid-xxxx")
# trade: TradingOperationResponse
```

---

### 7.4 `PUT /{trade_id}` — 更新交易状态

用于审批/拒绝交易操作。

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `status` | string | 是 | 新状态：`approved` / `rejected` |
| `reason` | string | 否 | 拒绝原因（rejected时必填） |

**客户端调用**：
```python
from src.schemas.trading import TradingOperationUpdate

# 注意：TradingClient 目前未单独封装 update 方法，可通过 HTTP 直接调用
# PUT /api/v1/trading/{trade_id}
```

---

## 8. 反馈复盘（Feedback）

所有接口前缀：`/api/v1/feedback`
客户端子模块：`client.feedback`

对交易决策进行事后复盘，形成"资讯→分析→交易→反馈→经验教训"的完整学习闭环。

### 8.1 `POST /` — 创建反馈记录

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `title` | string | 是 | 复盘标题 |
| `trigger_analysis_id` | UUID | 否 | 触发交易的分析ID |
| `trigger_trade_id` | UUID | 否 | 被复盘交易ID |
| `expected_outcome` | string | 否 | 当时预期结果 |
| `actual_outcome` | string | 否 | 实际发生结果 |
| `judgment_correct` | bool | 否 | 方向判断是否正确 |
| `error_reason` | string | 否 | 错误原因分析 |
| `missed_factors` | string | 否 | 遗漏的因素 |
| `adjustment_suggestions` | string | 否 | 后续调整建议 |
| `market_environment_snapshot` | object | 否 | 市场环境快照 |
| `lessons_learned` | string | 否 | 提炼的经验教训 |

**客户端调用**：
```python
from src.schemas.feedback import FeedbackCreate

fb = await client.feedback.create(FeedbackCreate(
    title="降准交易复盘-2024Q1",
    trigger_analysis_id=UUID("..."),
    trigger_trade_id=UUID("..."),
    expected_outcome="白酒板块上涨5-8%",
    actual_outcome="白酒板块上涨12%，超预期",
    judgment_correct=True,
    missed_factors="低估了外资流入带来的额外增量",
    lessons_learned="降准时点若与北向资金流入共振，弹性更大",
))
# fb: FeedbackResponse
```

---

### 8.2 `GET /` — 分页查询反馈列表

**查询参数**：`judgment_correct`（按判断正确性过滤）、`page`、`page_size`

**客户端调用**：
```python
result = await client.feedback.list(judgment_correct=False, page=1, page_size=20)

# 遍历全部
async for item in client.feedback.list_iter(judgment_correct=True):
    print(item["title"])
```

---

### 8.3 `GET /lessons` — 检索经验教训

从历史复盘记录中搜索经验教训。支持全文搜索。

**查询参数**：`search_text`（可选，搜索关键词）

**客户端调用**：
```python
lessons = await client.feedback.get_lessons(search_text="降准")
# lessons: list[dict] — 匹配的经验教训列表
```

---

### 8.4 `GET /{feedback_id}` — 获取单条反馈详情

**客户端调用**：
```python
fb = await client.feedback.get("uuid-xxxx")
# fb: FeedbackResponse
```

---

## 9. 搜索（Search）

所有接口前缀：`/api/v1/search`
客户端子模块：`client.search`

### 9.1 `POST /hybrid` — 混合检索

结合向量语义搜索、Elasticsearch全文搜索、结构化过滤三种方式，对知识库进行综合检索。结果包括资讯、分析和节点状态。

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query_text` | string | 是 | 搜索文本 |
| `filters` | object | 否 | 过滤条件：`info_type` / `source` / `date_from` / `date_to` / `entity_ids` / `node_type` / `min_importance` |
| `weights` | object | 否 | 权重配置 `{vector, fts, structural}`，默认 `{0.5, 0.3, 0.2}` |
| `limit` | int | 否 | 返回上限，默认 20 |
| `include_explanations` | bool | 否 | 是否返回各维度分数明细 |

**响应**：每条结果包含 `result_type`（`raw_information` / `analysis` / `node_state`）、标题、摘要片段、以及各维度评分明细。

**客户端调用**：
```python
from src.schemas.search import HybridSearchRequest

result = await client.search.hybrid(HybridSearchRequest(
    query_text="降准对白酒行业影响",
    filters={"info_type": "news", "min_importance": 0.5},
    weights={"vector": 0.6, "fts": 0.2, "structural": 0.2},
    limit=10,
    include_explanations=True,
))
# result: HybridSearchResponse
# result.items[0].score.vector  # 各维度得分
# result.items[0].result_type    # "raw_information" / "analysis" / "node_state"
```

---

### 9.2 `POST /hybrid/task` — 任务型搜索

根据特定任务类型（找证据、找相似、找关联节点、找历史分析）进行针对性搜索，不同的 `task_type` 对应不同的 `context` 参数。

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `task_type` | string | 是 | 任务类型：`find_evidence` / `find_similar` / `find_related_nodes` / `find_historical_analysis` |
| `context` | object | 否 | 任务上下文参数 |

**客户端调用**：
```python
from src.schemas.search import TaskSearchRequest

result = await client.search.task(TaskSearchRequest(
    task_type="find_evidence",
    context={"node_id": "...", "aspect": "driver"},
))
# result: dict
```

---

### 9.3 `POST /multi-granularity` — 多粒度搜索

同时在多个维度（原始资讯、分析、交易、反馈）中搜索，返回按粒度分组的结果。

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `search_text` | string | 是 | 搜索文本 |
| `granularities` | list | 否 | 搜索粒度，默认 `["raw_info", "analysis", "trading", "feedback"]` |
| `filters` | object | 否 | 全局过滤条件 |
| `limit_per_granularity` | int | 否 | 每粒度返回上限，默认 10 |

**客户端调用**：
```python
from src.schemas.search import MultiGranularityRequest

result = await client.search.multi_granularity(MultiGranularityRequest(
    search_text="茅台",
    granularities=["raw_info", "analysis", "trading", "feedback"],
    limit_per_granularity=10,
))
# result: MultiGranularityResponse
# result.granularities["raw_info"]["items"]   — 资讯结果
# result.granularities["analysis"]["items"]    — 分析结果
# result.granularities["trading"]["items"]     — 交易结果
# result.granularities["feedback"]["items"]    — 反馈结果
```

---

### 9.4 `POST /similar-cases` — 历史相似案例搜索

给定一个事件描述，在历史资讯库中找到最相似的案例及其关联的分析和复盘记录。用于"以史为鉴"——当类似事件发生时，参考历史决策和结果。

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `event_description` | string | 是 | 事件描述（向量语义匹配） |
| `event_type` | string | 否 | 事件类型过滤 |
| `affected_entities` | list[UUID] | 否 | 受影响的实体ID |
| `market_environment` | object | 否 | 当前市场环境描述 |
| `time_range` | object | 否 | 时间范围 `{from, to}` |

**客户端调用**：
```python
from src.schemas.search import SimilarCaseRequest

result = await client.search.similar_cases(SimilarCaseRequest(
    event_description="央行超预期降准，幅度0.5个百分点",
    event_type="monetary_policy",
    affected_entities=[UUID("...")],
    market_environment={"index_level": "high", "sentiment": "bullish"},
))
# result: SimilarCaseResponse
# result.similar_cases[0].similarity_score          — 相似度
# result.similar_cases[0].related_analyses          — 关联分析
# result.similar_cases[0].related_feedback          — 关联复盘
```

---

## 10. 处理流水线（Pipeline）

所有接口前缀：`/api/v1/pipeline`
客户端子模块：`client.pipeline`

管理资讯入库后的自动化处理流程（去重→实体提取→分析→节点挂载→评分）。

### 10.1 `GET /queue` — 查看处理队列

**查询参数**：`status`（按状态过滤）、`priority_min`（最低优先级）、`agent_assigned`（分配的Agent）、`page`、`page_size`

**客户端调用**：
```python
result = await client.pipeline.list_queue(status="pending", priority_min=5, page=1, page_size=20)

# 遍历全部
async for item in client.pipeline.list_queue_iter(status="error"):
    print(item["raw_info_id"], item["detail"])
```

---

### 10.2 `PUT /{raw_info_id}/status` — 更新处理状态

手动更新某条资讯的处理状态和优先级。

**请求体**：`status`（新状态）、`detail`（详情）、`priority`（优先级数值）

**客户端调用**：
```python
from src.schemas.pipeline import PipelineStatusUpdate

result = await client.pipeline.update_status("uuid-xxxx", PipelineStatusUpdate(
    status="processing",
    detail="重新分配至 Agent-3",
    priority=8,
))
```

---

### 10.3 `GET /stats` — 流水线统计

返回流水线各状态的数量分布、平均处理时长等统计信息。

**客户端调用**：
```python
stats = await client.pipeline.stats()
# stats: PipelineStats
```

---

### 10.4 `POST /reprioritize` — 批量调整优先级

**请求体**：`item_ids`（待调整的资讯ID列表）、`new_priority`（新优先级）

**客户端调用**：
```python
from src.schemas.pipeline import ReprioritizeRequest

result = await client.pipeline.reprioritize(ReprioritizeRequest(
    item_ids=[UUID("..."), UUID("...")],
    new_priority=10,
))
# result: {"updated": 2}
```

---

## 11. 时效管理（Validity）

所有接口前缀：`/api/v1/validity`
客户端子模块：`client.validity`

管理知识/分析/状态的有效期。金融市场的判断有明确时效性——某条分析可能在特定时间段内有效，过期后应标记为失效。

### 11.1 `POST /` — 创建时效记录

为某个目标（节点状态、分析、实体关系等）设置有效期。

**请求体**：`target_type`、`target_id`、`valid_from`、`valid_until`、`description`

**客户端调用**：
```python
from src.schemas.validity import TimeValidityCreate

v = await client.validity.create(TimeValidityCreate(
    target_type="node_state",
    target_id="uuid-xxxx",
    valid_from=datetime(2024, 1, 1),
    valid_until=datetime(2024, 6, 30),
    description="该分析基于Q1财报，有效期至半年报发布",
))
# v: TimeValidityResponse
```

---

### 11.2 `GET /` — 查询时效列表

**查询参数**：`target_type`（目标类型）、`expired`（是否已过期）

**客户端调用**：
```python
items = await client.validity.list(target_type="node_state", expired=False)
# items: list[TimeValidityResponse]
```

---

### 11.3 `PUT /{validity_id}/expire` — 提前标记失效

当某条知识被证明不再有效时，手动标记失效并记录失效原因。

**请求体**：`invalidation_reason`（失效原因）、`invalidation_evidence_id`（导致失效的新证据ID）

**客户端调用**：
```python
from src.schemas.validity import ValidityExpireRequest

result = await client.validity.expire("uuid-xxxx", ValidityExpireRequest(
    invalidation_reason="公司发布业绩修正公告，原预测失效",
    invalidation_evidence_id=UUID("..."),
))
# result: TimeValidityResponse
```

---

### 11.4 `PUT /{validity_id}/extend` — 延长有效期

当判断某条知识仍然有效时，延长其有效期。

**请求体**：`new_valid_until`（新的截止时间）

**客户端调用**：
```python
from src.schemas.validity import ValidityExtendRequest

result = await client.validity.extend("uuid-xxxx", ValidityExtendRequest(
    new_valid_until=datetime(2024, 12, 31),
))
# result: TimeValidityResponse
```

---

### 11.5 `GET /check` — 检查时效性

检查指定目标在当前时间是否仍有效。

**查询参数**：`target_type`、`target_id`

**客户端调用**：
```python
result = await client.validity.check(target_type="node_state", target_id="uuid-xxxx")
# result: ValidityCheckResponse
```

---

## 12. 冲突检测（Conflicts）

所有接口前缀：`/api/v1/conflicts`
客户端子模块：`client.conflicts`

当两条或多条资讯/分析对同一主题给出矛盾结论时，系统自动检测并记录冲突（如：一篇说"看多茅台"，另一篇说"看空茅台"）。

### 12.1 `POST /detect` — 检测冲突

对指定节点进行冲突检测，比较挂载在该节点下的不同资讯/分析的结论是否一致。

**请求体**：`node_id`（要检测的节点）、`target_type`（检测目标类型）

**客户端调用**：
```python
from src.schemas.conflict import ConflictDetectRequest

result = await client.conflicts.detect(ConflictDetectRequest(
    node_id=UUID("..."),
    target_type="state",
))
# result: ConflictDetectResponse(has_conflict=True, conflict_type="...", conflict_id=UUID(...))
```

---

### 12.2 `GET /` — 查询冲突列表

**查询参数**：`node_id`、`conflict_type`、`resolved`（是否已解决）、`page`、`page_size`

**客户端调用**：
```python
result = await client.conflicts.list(node_id=UUID("..."), resolved=False, page=1, page_size=20)

# 遍历全部
async for item in client.conflicts.list_iter(resolved=False):
    print(item["conflict_type"])
```

---

### 12.3 `PUT /{conflict_id}/resolve` — 解决冲突

对检测到的冲突给出解决方案（采纳某一方、综合判断、或标记为"待更多信息"）。

**请求体**：`resolution`（解决方案描述）

**客户端调用**：
```python
from src.schemas.conflict import ConflictResolveRequest

result = await client.conflicts.resolve("uuid-xxxx", ConflictResolveRequest(
    resolution="采纳看多观点，因为其基于更新的财报数据；看空观点基于的Q3数据已过时",
))
# result: ConflictResponse
```

---

## 13. 重要性排序（Ranking）

所有接口前缀：`/api/v1/ranking`
客户端子模块：`client.ranking`

对资讯/分析/节点进行多维度重要性评分，综合考虑信息的新颖性、来源权威度、影响范围、时效性等因素。

### 13.1 `POST /compute` — 计算/重新计算重要性

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `target_type` | string | 是 | 目标类型 |
| `target_id` | UUID | 是 | 目标ID |
| `force_recompute` | bool | 否 | 是否强制重算（忽略缓存） |

**客户端调用**：
```python
from src.schemas.ranking import RankingComputeRequest

result = await client.ranking.compute(RankingComputeRequest(
    target_type="raw_information",
    target_id=UUID("..."),
    force_recompute=True,
))
# result: RankingComputeResponse
```

---

### 13.2 `GET /` — 查看排序列表

**查询参数**：`target_type`（按类型过滤）、`min_score`（最低分数）、`limit`（返回数量，最大100）

**客户端调用**：
```python
rankings = await client.ranking.list(target_type="raw_information", min_score=0.7, limit=20)
# rankings: list[dict]
```

---

### 13.3 `GET /history/{target_type}/{target_id}` — 查看排序历史

追踪某个目标的评分随时间的变化。

**客户端调用**：
```python
history = await client.ranking.get_history("raw_information", "uuid-xxxx")
# history: list[RankingHistoryResponse]
```

---

## 14. 证据追溯（Evidence）

所有接口前缀：`/api/v1/evidence`
客户端子模块：`client.evidence`

从任意知识产物（节点状态、分析结论、交易决策）出发，沿证据链向上追溯到原始资讯来源，确保每一条结论都有据可查。

### 14.1 `GET /trace/{target_type}/{target_id}` — 追溯证据链

从某个目标（分析、节点状态等）出发，逐层向上追溯证据来源，直到原始资讯。

**查询参数**：`depth`（追溯深度，1~5，默认3）

**响应**：包含完整的证据树，每层列出父级来源及关系类型。

**客户端调用**：
```python
trace = await client.evidence.trace("node_state", "uuid-xxxx", depth=3)
# trace: EvidenceTraceResponse — 完整证据链
```

---

### 14.2 `GET /trace-node/{node_id}` — 追溯节点的证据全景

展示某个节点的完整证据图谱，包括支撑该节点当前状态的所有资讯、分析及其来源。

**查询参数**：`aspect`（可选，按方面过滤，如 `driver` / `risk` / `general`）

**客户端调用**：
```python
result = await client.evidence.trace_node("uuid-xxxx", aspect="driver")
# result: dict — 节点证据追溯结果
```

---

## 15. 时序查询（Queries）

所有接口前缀：`/api/v1/queries`
客户端子模块：`client.queries`

支持"截至某一时刻"的数据查询，用于回溯历史任意时间点的知识状态——"在那个时刻，我们知道了什么？"

### 15.1 `GET /as-of/{timestamp}` — 截至某时刻的状态查询

查询在指定历史时间点，某节点/实体/资讯的状态。反映的是"这个时间点我们掌握的信息"。

**查询参数**：`node_id` / `entity_id` / `info_id`（至少提供一个）

**客户端调用**：
```python
snapshot = await client.queries.as_of(
    timestamp=datetime(2024, 3, 15, 10, 0, 0),
    node_id=UUID("..."),
)
# snapshot: dict — 该时间点的状态快照
```

---

### 15.2 `POST /as-of-diff` — 两个时刻的状态差异对比

对比同一节点在两个时间点的状态差异，清晰展示"在这段时间内我们知道了什么新信息、观点发生了什么变化"。

**请求体**：`node_id`、`timestamp_a`、`timestamp_b`

**客户端调用**：
```python
diff = await client.queries.diff_state(
    node_id=UUID("..."),
    timestamp_a=datetime(2024, 1, 1),
    timestamp_b=datetime(2024, 3, 31),
)
# diff: dict — 两个时间点之间的差异
```

---

### 15.3 `GET /nodes/{node_id}/state/at/{timestamp}` — 节点在指定时刻的状态

返回节点在指定历史时刻的状态版本（模糊匹配到该时刻之前的最新版本）。

**客户端调用**：
```python
state = await client.queries.get_state_at("uuid-xxxx", timestamp=datetime(2024, 2, 1))
# state: dict — 节点在指定时刻的状态
```

---

## 16. 宏观报告（Macro Report）

所有接口前缀：`/api/v1/macro-report`
客户端子模块：`client.`（目前客户端尚未封装 MacroReport 子模块，可直接通过 HTTP 调用）

系统级宏观分析报告，聚合当前所有节点和资讯形成对整体市场环境的判断，支持增量更新（只记录变化的部分）。

### 16.1 `GET /current` — 获取当前宏观报告

返回最新的宏观分析报告。

---

### 16.2 `PUT /` — 更新宏观报告

**请求体**：
| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 是 | 报告正文 |
| `summary` | string | 否 | 摘要 |
| `changed_sections` | list | 否 | 变更的章节列表（增量更新） |

---

### 16.3 `GET /history` — 宏观报告历史

返回所有历史版本的宏观报告列表。

---

## 附录A：数据流概览

```
原始资讯录入 (POST /information)
    │
    ├── 去重检查 (POST /information/check-duplicate)
    ├── 合并重复 (POST /information/merge)
    │
    ├── 实体提取 (POST /information/{id}/extract-entities)
    │       │
    │       └── 实体关系 (POST /entities/relationships)
    │              │
    │              └── 影响路径 (GET /entities/impact-path/{id})
    │
    ├── 挂载到节点 (POST /nodes/{id}/attachments)
    │       │
    │       ├── 节点状态更新 (POST /nodes/{id}/state)
    │       ├── 状态压缩 (POST /nodes/{id}/compress)
    │       │
    │       ├── 冲突检测 (POST /conflicts/detect)
    │       ├── 重要性排序 (POST /ranking/compute)
    │       ├── 时效管理 (POST /validity)
    │       │
    │       ├── 分析记录 (POST /analysis)
    │       │       │
    │       │       └── 交易操作 (POST /trading)
    │       │               │
    │       │               └── 反馈复盘 (POST /feedback)
    │       │
    │       └── 证据追溯 (GET /evidence/trace/...)
    │
    ├── 混合搜索 (POST /search/hybrid)
    ├── 相似案例 (POST /search/similar-cases)
    ├── 时序查询 (GET /queries/as-of/...)
    │
    └── 宏观报告 (PUT /macro-report)
```

## 附录B：客户端-接口对应速查表

| 客户端方法 | HTTP 方法 | API 路径 |
|-----------|-----------|----------|
| `client.health()` | `GET` | `/health` |
| `client.information.ingest(data)` | `POST` | `/api/v1/information/` |
| `client.information.list(...)` | `GET` | `/api/v1/information/` |
| `client.information.list_iter(...)` | `GET` | `/api/v1/information/` (自动翻页) |
| `client.information.get(id)` | `GET` | `/api/v1/information/{id}` |
| `client.information.check_duplicate(data)` | `POST` | `/api/v1/information/check-duplicate` |
| `client.information.merge(data)` | `POST` | `/api/v1/information/merge` |
| `client.information.extract_entities(id, data)` | `POST` | `/api/v1/information/{id}/extract-entities` |
| `client.information.get_entities(id)` | `GET` | `/api/v1/information/{id}/entities` |
| `client.entities.create(data)` | `POST` | `/api/v1/entities/` |
| `client.entities.list(...)` | `GET` | `/api/v1/entities/` |
| `client.entities.list_iter(...)` | `GET` | `/api/v1/entities/` (自动翻页) |
| `client.entities.create_relationship(data)` | `POST` | `/api/v1/entities/relationships` |
| `client.entities.get_relationships(id)` | `GET` | `/api/v1/entities/{id}/relationships` |
| `client.entities.impact_path(id, ...)` | `GET` | `/api/v1/entities/impact-path/{id}` |
| `client.nodes.create(data)` | `POST` | `/api/v1/nodes/` |
| `client.nodes.list(...)` | `GET` | `/api/v1/nodes/` |
| `client.nodes.list_iter(...)` | `GET` | `/api/v1/nodes/` (自动翻页) |
| `client.nodes.get(id)` | `GET` | `/api/v1/nodes/{id}` |
| `client.nodes.attach(id, data)` | `POST` | `/api/v1/nodes/{id}/attachments` |
| `client.nodes.get_attachments(id, ...)` | `GET` | `/api/v1/nodes/{id}/attachments` |
| `client.nodes.get_current_state(id)` | `GET` | `/api/v1/nodes/{id}/state/current` |
| `client.nodes.update_state(id, data)` | `POST` | `/api/v1/nodes/{id}/state` |
| `client.nodes.get_state_history(id)` | `GET` | `/api/v1/nodes/{id}/state/history` |
| `client.nodes.compress(id, data)` | `POST` | `/api/v1/nodes/{id}/compress` |
| `client.analysis.create(data)` | `POST` | `/api/v1/analysis/` |
| `client.analysis.list(...)` | `GET` | `/api/v1/analysis/` |
| `client.analysis.list_iter(...)` | `GET` | `/api/v1/analysis/` (自动翻页) |
| `client.analysis.get(id)` | `GET` | `/api/v1/analysis/{id}` |
| `client.trading.create(data)` | `POST` | `/api/v1/trading/` |
| `client.trading.list(...)` | `GET` | `/api/v1/trading/` |
| `client.trading.list_iter(...)` | `GET` | `/api/v1/trading/` (自动翻页) |
| `client.trading.get(id)` | `GET` | `/api/v1/trading/{id}` |
| `client.feedback.create(data)` | `POST` | `/api/v1/feedback/` |
| `client.feedback.list(...)` | `GET` | `/api/v1/feedback/` |
| `client.feedback.list_iter(...)` | `GET` | `/api/v1/feedback/` (自动翻页) |
| `client.feedback.get_lessons(...)` | `GET` | `/api/v1/feedback/lessons` |
| `client.feedback.get(id)` | `GET` | `/api/v1/feedback/{id}` |
| `client.search.hybrid(data)` | `POST` | `/api/v1/search/hybrid` |
| `client.search.task(data)` | `POST` | `/api/v1/search/hybrid/task` |
| `client.search.multi_granularity(data)` | `POST` | `/api/v1/search/multi-granularity` |
| `client.search.similar_cases(data)` | `POST` | `/api/v1/search/similar-cases` |
| `client.pipeline.list_queue(...)` | `GET` | `/api/v1/pipeline/queue` |
| `client.pipeline.list_queue_iter(...)` | `GET` | `/api/v1/pipeline/queue` (自动翻页) |
| `client.pipeline.update_status(id, data)` | `PUT` | `/api/v1/pipeline/{id}/status` |
| `client.pipeline.stats()` | `GET` | `/api/v1/pipeline/stats` |
| `client.pipeline.reprioritize(data)` | `POST` | `/api/v1/pipeline/reprioritize` |
| `client.validity.create(data)` | `POST` | `/api/v1/validity/` |
| `client.validity.list(...)` | `GET` | `/api/v1/validity/` |
| `client.validity.expire(id, data)` | `PUT` | `/api/v1/validity/{id}/expire` |
| `client.validity.extend(id, data)` | `PUT` | `/api/v1/validity/{id}/extend` |
| `client.validity.check(...)` | `GET` | `/api/v1/validity/check` |
| `client.conflicts.detect(data)` | `POST` | `/api/v1/conflicts/detect` |
| `client.conflicts.list(...)` | `GET` | `/api/v1/conflicts/` |
| `client.conflicts.list_iter(...)` | `GET` | `/api/v1/conflicts/` (自动翻页) |
| `client.conflicts.resolve(id, data)` | `PUT` | `/api/v1/conflicts/{id}/resolve` |
| `client.ranking.compute(data)` | `POST` | `/api/v1/ranking/compute` |
| `client.ranking.list(...)` | `GET` | `/api/v1/ranking/` |
| `client.ranking.get_history(type, id)` | `GET` | `/api/v1/ranking/history/{type}/{id}` |
| `client.evidence.trace(type, id, ...)` | `GET` | `/api/v1/evidence/trace/{type}/{id}` |
| `client.evidence.trace_node(id, ...)` | `GET` | `/api/v1/evidence/trace-node/{id}` |
| `client.queries.as_of(ts, ...)` | `GET` | `/api/v1/queries/as-of/{ts}` |
| `client.queries.diff_state(...)` | `POST` | `/api/v1/queries/as-of-diff` |
| `client.queries.get_state_at(id, ts)` | `GET` | `/api/v1/queries/nodes/{id}/state/at/{ts}` |
