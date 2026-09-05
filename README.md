# Quant Workflow

金融资讯分析与模拟交易流水线 —— 从资讯采集到交易决策的 AI Agent 自动化系统。

## 系统定位：量化投研闭环中的「AI 分析层」

本仓库不是独立系统，而是「**量化投研闭环**」中的 **AI 分析层**：

| 仓库 | 角色 | 关系 |
| --- | --- | --- |
| [kbquant](https://github.com/Iamqlwy/kbquant) | 知识库后端（数据/知识层，共享大脑） | 本仓库从它的 processing_queue 拉取资讯，并把 Analysis/Trading/Feedback 写回 |
| workflow（本仓库） | 多 Agent 分析流水线 | 重要性分级 → 深度分析 → 风控 → 复盘 |
| [Trade-system](https://github.com/Iamqlwy/Trade-system) | A 股交易平台（应用/交易层） | 读取 kbquant 沉淀的知识做可视化与交易 |

```text
                        ┌──────────────────────────────────────────────┐
                        │          量化投研闭环（一体系统）              │
                        └──────────────────────────────────────────────┘

   资讯源                   数据/知识层                  AI 分析层               应用/交易层
 (新闻/研报/           ┌───────────────────┐ 写回    ┌──────────────────┐ 读取 ┌──────────────────┐
  社交媒体/CSV) ─────▶ │  kbquant          │ ◀───── │  workflow        │ ───▶ │  Trade-system    │
                      │  知识库后端        │ 消费队列│  多 Agent 流水线 │ DB/ES│  A股交易平台      │
                      │  PG(pgvector)+ES  │        │  重要性分级→深度  │      │  FastAPI+Vue3    │
                      │  +PgBouncer       │        │  分析→风控→复盘   │      │  虚拟账户/实盘    │
                      │  WorldNode/分析/  │        │  (SQLite 本地态)  │      │  知识图谱/检索    │
                      │  交易/复盘/混合搜索│        │                  │      │  LLM 交易助手    │
                      └───────────────────┘        └──────────────────┘      └──────────────────┘
                         ▲ 共享大脑/唯一事实源
                         │  (只读账号: kbquant_readonly / kbquant_es_readonly)
                                                                          ◄ 本仓库 = AI 分析层
```

> **数据流**：资讯流入 → kbquant 入库 → 本仓库分析 → 知识沉淀回 kbquant → Trade-system 可视化/交易。
>
> **启动顺序**：先启动 [kbquant](https://github.com/Iamqlwy/kbquant)（数据层就绪后本仓库才能消费队列），再启动本仓库，最后启动 [Trade-system](https://github.com/Iamqlwy/Trade-system)。

## 概述

这是一个基于 LLM Agent 的量化投研自动化工具。系统接收金融资讯（新闻、研报、社交媒体等），通过多层 AI Agent 流水线自动完成：资讯重要性分级 → 深度分析 → 风控审核 → 交易建议 → 定时复盘，同时支持宏观分析、条件触发器、偏好学习等辅助功能。

**核心思路**：让 AI 模仿人类分析师的投研流程 —— 读资讯、查资料、看盘面、做判断、写报告、复盘反思。

## 架构

```
资讯输入 (KB API / CSV)
    │
    ▼
┌──────────────────────────────────────────────┐
│              PipelineOrchestrator             │
│                   流水线引擎                    │
│                                              │
│  INGESTED → 重要性判断 → DEEP_ANALYZING      │
│     → DEEP_ANALYZED → RISK_CHECKING          │
│     → REFLECTION_PENDING → REFLECTION_EXECUTING│
│                                              │
│  宏观分支:                                     │
│     → MACRO_URGENT (紧急)                     │
└──────────────┬───────────────────────────────┘
               │
    ┌──────────┼──────────┐
    ▼          ▼          ▼
┌────────┐ ┌────────┐ ┌──────────┐
│ Deep   │ │ Risk   │ │Reflection│
│Analysis│ │Control │ │ Agent    │
│ Agent  │ │ Agent  │ │          │
└────────┘ └────────┘ └──────────┘
    │          │          │
    ▼          ▼          ▼
┌──────────────────────────────────────────────┐
│            KB API (知识库后端)                 │
│  WorldNode / Analysis / Trading / Feedback   │
│  Search / Evidence / Ranking / Conflicts     │
└──────────────────────────────────────────────┘
    │
    ▼
┌──────────────┐  ┌─────────────────┐
│ TriggerEngine│  │ MarketDataProvider│
│ 条件触发器轮询 │  │ 行情数据 (xtquant  │
│              │  │ + 本地CSV)       │
└──────────────┘  └─────────────────┘
```

## 目录结构

```
workflow/
├── src/
│   ├── main.py                  # 主入口 WorkflowApp
│   ├── config.py                # 全局配置 (pydantic-settings)
│   ├── db.py                    # SQLite 异步引擎
│   ├── core/
│   │   └── clock.py             # 统一时钟 (实盘/模拟共用)
│   ├── llm/
│   │   └── client.py            # LLM 客户端 (DeepSeek/Qwen, OpenAI 兼容)
│   ├── models/
│   │   └── tables.py            # 本地 SQLite 表 (Task/Trigger/Preference)
│   ├── ingestion/
│   │   ├── poller.py            # KB 队列消费者
│   │   └── csv_loader.py        # CSV 模拟数据加载器
│   ├── pipeline/
│   │   ├── orchestrator.py      # 流水线编排引擎
│   │   ├── states.py            # 任务状态机 (TaskState 枚举)
│   │   ├── significance.py      # 个股/行业重要性评分
│   │   └── macro_significance.py# 宏观资讯紧急度评估
│   ├── agents/
│   │   ├── base.py              # StageAgent 基类 (手动 ReAct 循环)
│   │   ├── deep_analysis.py     # 深度分析 Agent (4 阶段)
│   │   ├── risk_control.py      # 风控审核 Agent (2 阶段)
│   │   ├── reflection.py        # 复盘 Agent (4 阶段 + Tree of Thought)
│   │   └── macro_daily.py       # 宏观 Agent (urgent/daily 共用, 3 阶段)
│   ├── tools/                   # LangChain 工具集
│   │   ├── _deps.py             # 共享依赖 (contextvars)
│   │   ├── knowledge.py         # 知识库检索工具
│   │   ├── market.py            # 实时行情工具
│   │   ├── history.py           # 历史数据/图表工具
│   │   ├── review.py            # 分析/交易/节点查询工具
│   │   ├── writer.py            # 写入工具 (创建分析/交易/触发器/复盘)
│   │   ├── macro.py             # 宏观报告工具
│   │   └── stages.py            # 各 Agent 阶段的工具子集分配
│   ├── market/
│   │   ├── data.py              # MarketDataProvider (行情核心)
│   │   ├── indexer.py           # 1m CSV 字节偏移索引
│   │   ├── loader.py            # CSV 数据加载器
│   │   ├── tick_aggregator.py   # tick → 1m 聚合器
│   │   ├── schemas.py           # 常量定义 (指数/粒度/列名映射)
│   │   └── charts.py            # 技术分析图表生成 (matplotlib)
│   ├── triggers/
│   │   ├── engine.py            # 触发器后台轮询引擎
│   │   ├── compiler.py          # NL→条件树编译器 (两模型串行)
│   │   ├── atoms.py             # 触发原子定义 & 条件树评估
│   │   └── evaluators/          # 各类原子评估器实现
│   └── preferences/
│       └── manager.py           # 偏好管理器 (结构参数 + 行业认知)
├── tests/                       # 测试文件
├── update/                      # 数据更新脚本
├── data/                        # 本地数据库 & JSON 数据
├── docs/
│   ├── API.md                   # KB API 接口文档
│   └── kb_api_requirements.md   # KB API 需求说明
├── pyproject.toml
└── .env                         # 环境变量配置
```

## 核心组件

### 1. 流水线引擎 (PipelineOrchestrator)

状态机驱动，每轮 tick 从数据库拉取 pending 任务，按状态分派给对应 Agent：

```
CRAWLED → INGESTED → SIGNIFICANCE_CHECKING ──→ DEEP_ANALYZING → DEEP_ANALYZED
                          │                         │
                          │ (非重要)                  │
                          ▼                         ▼
                       SKIPPED              RISK_CHECKING
                                                │
                                    ┌───────────┴───────────┐
                                    ▼                       ▼
                              TRADE_CREATED             REJECTED
                                    │                       │
                                    └───────────┬───────────┘
                                                ▼
                                        REFLECTION_PENDING
                                                │
                                                ▼
                                        REFLECTION_EXECUTING
                                                │
                                                ▼
                                        REFLECTION_COMPLETE

宏观分支:
  MACRO_URGENT → MACRO_REPORT_UPDATED
```

### 2. Agent 体系

所有 Agent 基于 `StageAgent` 基类，采用**分阶段 ReAct 循环**：

- LLM 不产出 tool_call → 当前阶段完成 → 自动进入下一阶段
- 消息列表跨阶段继承，LLM 能看到完整历史
- 每个阶段有独立的 system prompt 和可用工具集

| Agent | 阶段 | 用途 |
|-------|------|------|
| **DeepAnalysis** | 知识库检索 → 市场数据采集 → 综合分析 → 结果落地 | 对重要资讯做深度分析，生成交易建议 |
| **RiskControl** | 盘面验证 → 审批决策 | 验证分析逻辑是否被当前盘面支持 |
| **Reflection** | 回顾原始分析 → 市场验证 → Tree of Thought 复盘 → 结果落地 | 定时复盘，总结经验教训 |
| **Macro** | 背景检索 → 综合研判 → 更新报告 | 紧急宏观资讯即时分析（无日报，宏观摘要自动注入风控上下文） |

### 3. StageAgent 基类

手动控制的 ReAct 循环（不依赖 LangChain AgentExecutor）：

- 使用 `contextvars` 管理并发安全的依赖注入（QuantClient、MarketDataProvider、PreferenceManager）
- Session 级短引用注册表（A1/T1/F1）让 Agent 在工具调用间追踪实体关系
- 支持 DeepSeek 和 Qwen 两种 LLM provider

### 4. 知识库 (KB API)

系统依赖一个独立运行的 KB（Knowledge Base）后端服务，提供：

- **资讯管理**：录入、去重、实体提取
- **WorldNode**：投资标的/主题的状态管理（树形层级，支持版本历史）
- **分析记录**：存储 Agent 产出的分析报告
- **交易操作**：记录买卖/止损/止盈等交易建议
- **反馈复盘**：记录复盘结论和经验教训
- **搜索**：混合搜索（向量 + 全文 + 结构化）、相似案例检索
- **其他**：冲突检测、时效管理、证据追溯、重要性排序、宏观报告

详见 `docs/API.md`。

> 该 KB 即 [kbquant](https://github.com/Iamqlwy/kbquant) 仓库（闭环的数据/知识层），不是本仓库内建组件；其沉淀的知识进一步被 [Trade-system](https://github.com/Iamqlwy/Trade-system) 读取用于可视化与交易。

### 5. 行情数据 (MarketDataProvider)

双模式行情提供：

- **实盘模式**：通过 xtquant 订阅全市场 tick，实时回调 + tick→1m 聚合
- **CSV 模式**：从本地 `C:/klines/` 读取预下载的历史数据（支持模拟回测）

关键能力：
- 多粒度 K 线查询（1m/5m/15m/30m/60m/1d/1w/1M）
- 板块流通市值加权日内走势（预计算缓存，O(1) 查询）
- 技术指标计算（MA/RSI/MACD/布林带/KDJ/量比）
- 涨跌停实时判断（不含未来信息泄露）
- 市场宽度 / 指数纵览 / 板块龙头识别

### 6. 触发器系统

两模型串行的 NL→条件编译流水线：

1. **LLM A** 将自然语言编译为 AND/OR 条件树
2. **板块名校验**：自动纠正 LLM 输出的板块名拼写错误
3. **LLM B** 评审编译结果，不通过则返回 A 修改
4. **动作解析**：将"触发后做什么"转为结构化动作（deep_analysis / trade）

支持 30+ 种触发原子，覆盖：
- 价格与成交量（price_level, volume_spike, turnover_rate 等）
- 技术指标（ma_cross, macd, rsi, bollinger, kdj 等）
- 市场情绪（market_breadth, market_volume 等）
- 板块分析（sector_index_change, sector_leader_strength 等）
- 日内分时形态（冲高回落、探底回升、A字/V字形态、单边走势）
- 时间约束（time_after, time_window, time_before）

引擎每秒轮询所有 waiting 状态的触发器，批量预热行情数据后评估。

### 7. 运行模式

**实盘模式** (`simulation_enabled=false`)：
- ConsumerPoller 轮询 KB 队列获取新资讯
- MarketDataProvider 从 xtquant 获取实时行情
- 真实时钟推进

**模拟模式** (`simulation_enabled=true`)：
- CSVNewsLoader 按时间窗口分批推入资讯到 KB
- MarketDataProvider 从本地 CSV 读取行情（含时钟截断防未来数据泄露）
- 可通过 `simulation_tick_duration_minutes` 控制时间流速
- 退出条件：CSV 读完 + 所有任务处理完毕

### 8. 偏好系统 (PreferenceManager)

持久化的可学习偏好，存储在本地 SQLite：

- **结构化参数**：板块权重、风控参数、分析偏好
- **行业认知文本**：每个行业的碎片化观察笔记，增量追加
- **自动重写**：追加次数达阈值后通过 LLM 全量重写为精炼文本
- **复盘驱动更新**：复盘 Agent 可建议调整权重和参数（带步长/边界约束）

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.11+ |
| LLM | DeepSeek / Qwen (OpenAI 兼容 API) |
| Agent 框架 | LangChain + LangGraph (手动 ReAct 循环) |
| 异步 | asyncio + aiosqlite |
| 数据库 | SQLite (aiosqlite + SQLAlchemy async) |
| 行情 | xtquant (实盘) + pandas (CSV) |
| 图表 | matplotlib |
| 配置 | pydantic-settings (.env) |

## 快速开始

### 前置条件

1. **KB API 服务**：先启动 [kbquant](https://github.com/Iamqlwy/kbquant) 后端（默认 `http://localhost:8000`，即 `KB_API_BASE_URL`）。kbquant 是本系统的数据源头：本仓库通过 `kbquant.client.QuantClient` 消费其队列并把分析结果写回。
2. **行情数据**：本地 `C:/klines/` 目录下有 CSV 格式的 K 线数据（或安装 xtquant 用于实盘）
3. **LLM API Key**：DeepSeek 或 Qwen 的 API Key

### 安装

```bash
pip install -e .
# 或
pip install -e ".[dev]"
```

### 配置

编辑 `.env` 文件，至少配置：

```env
KB_API_BASE_URL=http://localhost:8000/api/v1
KB_API_KEY=your-kb-api-key
LLM_PROVIDER=deepseek    # 或 qwen
DEEPSEEK_API_KEY=sk-xxx  # 如果用 DeepSeek
```

### 运行

```bash
# 实盘模式
python -m src.main

# 模拟模式（需设置 .env 中 SIMULATION_ENABLED=true）
python -m src.main
```

### 运行测试

```bash
pytest tests/ -v
```

## 相关仓库

- [kbquant](https://github.com/Iamqlwy/kbquant) — 知识库后端（本仓库的上游数据源与下游落库处）
- [Trade-system](https://github.com/Iamqlwy/Trade-system) — A 股交易平台（知识落库后的展示/交易层）

## 关键依赖

- `kbquant` — KB API Python 客户端 SDK（内部包）
- `xtquant` — QMT 量化交易平台 Python SDK（实盘行情，可选）
- `langchain` / `langchain-openai` / `langgraph` — Agent 框架
- `sqlalchemy[asyncio]` / `aiosqlite` — 异步数据库
- `akshare` — A 股数据接口（用于数据更新脚本）
- `pandas` / `numpy` / `matplotlib` — 数据处理与可视化

## 数据更新

`update/` 目录包含各类数据更新脚本：

- `update_daily_all.py` — 更新全部股票日线
- `update_single_stock.py` — 更新单只股票
- `update_all_sector.py` — 更新全板块数据
- `update_concept_klines.py` — 更新概念板块 K 线
- `update_zdt.py` — 更新涨跌停数据
- `update_data.py` — 通用数据更新
- `supplement_stock_1m.py` / `supplement_index_1m.py` — 补充 1 分钟线
