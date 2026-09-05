"""NL → 触发条件树 + 动作解析（A编译 → 统一校验 → 名称转代码）

条件树格式 —— 无限嵌套的 AND/OR 树：

每个节点是以下两种之一：
1. 逻辑组：{{"logic": "AND"|"OR", "children": [node, node, ...]}}
2. 原子叶：{{"atom": "atom_name", "params": {{"key": value, ...}}}}

时间原子（time_after/time_window/time_before）是普通的原子叶，
和其他原子一样放在 children 里，统一由 LLM 编译。

编译时 LLM 使用中文股票名/指数名（如"贵州茅台"、"上证指数"），
编译完成后转换为 ticker 代码存储。
"""

from __future__ import annotations

import ast  # noqa: F811, I001
import json
from copy import deepcopy
from datetime import datetime, timedelta
from difflib import get_close_matches
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
from loguru import logger

from src.config import settings
from src.core.timezone import BEIJING_TZ
from src.llm.provider import LLMProvider
from src.observability import safe_observation_value, start_observation
from src.tools._deps import _is_transient_tool_error
from src.triggers.atoms import (
    ATOM_DEFINITIONS,
    ATOM_SCHEMA,
    format_atom_list_for_prompt,
    normalize_and_validate_tree,
)
from src.triggers.evaluators import EVALUATORS

if TYPE_CHECKING:
    from src.market import MarketDataProvider

# ── 板块名校验 ──────────────────────────────

_SECTOR_ATOMS = {
    name for name, schema in ATOM_SCHEMA.items() if schema.get("sector_data_needs") or schema.get("member_ticker_needs")
}


@lru_cache(maxsize=1)
def _load_sector_names() -> list[str]:
    """从本地 CSV 加载所有合法板块名，按长度排序"""
    names: set[str] = set()
    klines = Path(settings.klines_path)
    for csv_name in ["concept_filter.csv", "industry.csv", "industry_children.csv"]:
        path = klines / "concepts" / csv_name
        if not path.exists():
            continue
        df = pd.read_csv(path, dtype=str)
        for col in ["name", "child_name"]:
            if col in df.columns:
                names.update(df[col].dropna().unique().tolist())
    return sorted(names, key=lambda x: len(x))


def _format_sector_list() -> str:
    """格式化板块列表为紧凑字符串（约 4KB，适合放入 prompt）"""
    names = _load_sector_names()
    return "、".join(names)


def _find_sector_corrections(tree: dict) -> dict[str, list[str]]:
    """检查板块名，返回不匹配的 {原始名: [候选名列表]}"""
    known = set(_load_sector_names())
    if not known:
        return {}

    corrections: dict[str, list[str]] = {}
    for name, _field in _collect_sector_names(tree):
        if name in known:
            continue
        candidates = get_close_matches(name, known, n=5, cutoff=0.4)
        corrections[name] = candidates if candidates else ["__NO_MATCH__"]
    return corrections


def _collect_sector_names(tree: dict) -> list[tuple[str, str]]:
    """遍历条件树，收集所有板块原子的 (name, field)"""
    found: list[tuple[str, str]] = []

    def _walk(node: dict) -> list[dict]:
        if "atom" in node and node["atom"] in _SECTOR_ATOMS:
            params = node.get("params", {})
            for key in ("sector", "sector_a", "sector_b"):
                val = params.get(key, "")
                if val:
                    found.append((val, key))
        for child in node.get("children", []):
            _walk(child)

    _walk(tree)
    return found


def _apply_sector_mapping(tree: dict, mapping: dict[str, str]) -> None:
    """在条件树中原地替换板块名"""

    def _walk(node: dict) -> list[dict]:
        if "atom" in node and node["atom"] in _SECTOR_ATOMS:
            params = node.get("params", {})
            for key in ("sector", "sector_a", "sector_b"):
                val = params.get(key, "")
                if val in mapping:
                    params[key] = mapping[val]
        for child in node.get("children", []):
            _walk(child)

    _walk(tree)


# ── 原子名校验 ──────────────────────────────


def _collect_atom_names(tree: dict) -> set[str]:
    """遍历条件树，收集所有原子名"""
    names: set[str] = set()

    def _walk(node: dict) -> list[dict]:
        if "atom" in node:
            names.add(node["atom"])
        for child in node.get("children", []):
            _walk(child)

    _walk(tree)
    return names


def _find_atom_corrections(tree: dict) -> dict[str, list[str]]:
    """检查原子名，返回不匹配的 {原始名: [候选名列表]}"""
    valid_names = list(EVALUATORS.keys())
    if not valid_names:
        return {}

    corrections: dict[str, list[str]] = {}
    for name in _collect_atom_names(tree):
        if name in EVALUATORS:
            continue
        if name in _TIME_ATOMS_FOR_EXTRACT:
            continue  # meta 原子，由 compiler 直接提取，不送入 evaluator
        candidates = get_close_matches(name, valid_names, n=5, cutoff=0.4)
        corrections[name] = candidates if candidates else ["__NO_MATCH__"]
    return corrections


def _apply_atom_mapping(tree: dict, mapping: dict[str, str]) -> None:
    """在条件树中原地替换原子名"""

    def _walk(node: dict) -> list[dict]:
        if "atom" in node and node["atom"] in mapping:
            node["atom"] = mapping[node["atom"]]
        for child in node.get("children", []):
            _walk(child)

    _walk(tree)


# ── 股票/指数名称校验与转换 ──────────────────────


def _ensure_action_ticker(action: dict, tree: dict) -> None:
    """若 buy/sell 动作缺少 ticker，从条件树中回退提取第一个 ticker。

    场景：LLM 在 action_nl 中只写了"买入100股"没写标的，但 condition_nl
    中已声明了 ticker。直接从编译好的条件树中提取，不再依赖 LLM 补全。
    """
    action_type = action.get("action_type", "")
    if action_type not in ("buy", "sell"):
        return
    params = action.get("action_params", {})
    if params.get("ticker"):
        return
    ticker_names = _collect_ticker_names(tree)
    if ticker_names:
        # 取第一个（已是中文名称，待后续 _convert_action_names_to_codes 转代码）
        fallback = next(iter(ticker_names))
        params["ticker"] = fallback


def _collect_ticker_names(tree: dict) -> set[str]:
    """遍历条件树，收集所有 params['ticker'] 的值（股票名或指数名）"""
    names: set[str] = set()

    def _walk(node: dict) -> list[dict]:
        if "atom" in node:
            ticker = node.get("params", {}).get("ticker", "")
            if ticker:
                names.add(ticker)
        for child in node.get("children", []):
            _walk(child)

    _walk(tree)
    return names


def _find_ticker_name_corrections(tree: dict, market: MarketDataProvider) -> dict[str, list[str]]:
    """检查 ticker 参数值是否为合法的股票名或指数名。

    跳过已有 '.' 的值（已是 ticker 代码）。
    返回 {原始名: [候选列表]}，候选为 ["__NO_MATCH__"] 表示完全无法解析。
    market 为 None 时返回空字典。
    """
    if market is None:
        return {}

    corrections: dict[str, list[str]] = {}
    for name in _collect_ticker_names(tree):
        name = name.strip()
        if "." in name:
            continue
        # 先查指数名
        if market.resolve_index_name(name) is not None:
            continue
        # 再查股票名
        matches = market.resolve_stock_ticker(name)
        if matches:
            if len(matches) > 1:
                options = [f"{n}({c})" for n, c in matches]
                corrections[name] = options
        else:
            corrections[name] = ["__NO_MATCH__"]
    return corrections


def _apply_ticker_mapping(tree: dict, mapping: dict[str, str]) -> None:
    """在条件树中原位替换 ticker 名称。"""

    def _walk(node: dict) -> None:
        if "atom" in node:
            params = node.get("params", {})
            ticker = params.get("ticker", "")
            if ticker in mapping:
                params["ticker"] = mapping[ticker]
        for child in node.get("children", []):
            _walk(child)

    _walk(tree)


def _convert_names_to_codes(tree: dict, market: MarketDataProvider) -> tuple[dict, list[str]]:
    """将条件树中所有中文股票名/指数名替换为 ticker 代码。

    用于编译完成后、存储前。不修改原树。
    market 为 None 时返回原树副本。

    Returns:
        (converted_tree, unresolved_names) — 无法解析的名称列表
    """
    tree = deepcopy(tree)
    if market is None:
        return tree, []

    unresolved: list[str] = []

    def resolve(name: str) -> str:
        if "." in name:
            return name
        code = market.resolve_index_name(name)
        if code:
            return code
        matches = market.resolve_stock_ticker(name)
        if matches:
            return matches[0][0]
        unresolved.append(name)
        return name

    def _walk(node: dict) -> list[dict]:
        if "atom" in node:
            params = node.get("params", {})
            if "ticker" in params:
                params["ticker"] = resolve(params["ticker"])
        for child in node.get("children", []):
            _walk(child)

    _walk(tree)
    return tree, unresolved


def _convert_action_names_to_codes(action: dict, market: MarketDataProvider) -> dict:
    """将 action_params 中的 ticker 由名称转为代码（如果存在）。不修改原 dict。"""
    if market is None:
        return action
    params = action.get("action_params", {})
    ticker = params.get("ticker", "")
    if not ticker or "." in ticker:
        return action
    code = market.resolve_index_name(ticker)
    if code is None:
        matches = market.resolve_stock_ticker(ticker)
        if matches:
            code = matches[0][0]
    if code:
        action = deepcopy(action)
        action["action_params"]["ticker"] = code
    return action


_TIME_ATOMS_FOR_EXTRACT = {"time_after", "time_window", "time_before"}


def _extract_time_window(tree: dict, now: datetime) -> tuple[str | None, str | None]:
    """从条件树提取时间窗口，返回 (not_before_iso, not_after_iso)。

    只在顶层 AND 的子节点中提取，避免 OR 污染。
    例如 "涨停 OR 三天后提醒" —— time_after 在 OR 分支中，不提取。
    """
    lower_bounds: list[datetime] = []
    upper_bounds: list[datetime] = []

    # 只在顶层 AND 中提取
    if tree.get("logic") == "AND":
        for child in tree.get("children", []):
            if isinstance(child, dict) and child.get("atom") in _TIME_ATOMS_FOR_EXTRACT:
                params = child.get("params", {})
                name = child["atom"]
                try:
                    if name == "time_after" and "days" in params:
                        lower_bounds.append(now + timedelta(days=params["days"]))
                    elif name == "time_window":
                        if "days_min" in params:
                            lower_bounds.append(now + timedelta(days=params["days_min"]))
                        if "days_max" in params:
                            upper_bounds.append(now + timedelta(days=params["days_max"]))
                    elif name == "time_before" and "days" in params:
                        upper_bounds.append(now + timedelta(days=params["days"]))
                except (TypeError, ValueError) as e:
                    logger.warning("时间原子参数类型错误: atom={}, params={}, error={}", name, params, e)

    not_before = max(lower_bounds).isoformat() if lower_bounds else None
    not_after = min(upper_bounds).isoformat() if upper_bounds else None

    if not_before and not_after and not_before > not_after:
        logger.warning(
            "时间窗口矛盾: not_before={}, not_after={}，下界 > 上界，触发将永不命中",
            not_before,
            not_after,
        )

    return not_before, not_after


def _strip_time_atoms(tree: dict) -> dict:
    """从条件树中移除时间原子（它们已提取为 not_before/not_after）。

    返回新树，不修改原树。
    """
    tree = deepcopy(tree)
    if tree.get("logic") == "AND":
        tree["children"] = [
            c
            for c in tree.get("children", [])
            if not (isinstance(c, dict) and c.get("atom") in _TIME_ATOMS_FOR_EXTRACT)
        ]
        # 如果 children 只剩一个，提升它
        if len(tree["children"]) == 1:
            return tree["children"][0]
    return tree


# ── 路径标注（编译时预计算，避免 engine 每轮 deep copy）──


def _annotate_tree_paths(tree: dict) -> dict:
    """给条件树的每个原子叶打上 _path 标记，供 evaluate_condition_tree 使用。

    路径格式与 engine._collect_atoms 一致，编译时标注后写入 DB，
    engine 评估时直接使用，无需再 deep copy。

    返回新树，不修改原树。
    """

    def _walk(node: ast.AST, prefix: str) -> list[dict]:
        if "atom" in node:
            node["_path"] = prefix
        for i, child in enumerate(node.get("children", [])):
            child_prefix = f"{prefix}.{i}" if prefix else str(i)
            _walk(child, child_prefix)

    tree = deepcopy(tree)
    if "logic" in tree and "children" in tree:
        for i, child in enumerate(tree["children"]):
            _walk(child, str(i))
    elif "atom" in tree:
        _walk(tree, "0")
    return tree


# ── 条件编译 prompts ─────────────────────────

COMPILER_A_PROMPT = """你是一个专业的交易条件编译器。将自然语言描述的交易条件转换为结构化的触发原子组合树。

## 核心职责
- 将用户的交易触发条件转换为可执行的JSON格式条件树
- 只使用可用的触发原子（见下方列表）
- **增强优先**：对模糊、简短、缺参数的输入主动补全，而非拒绝
- 优先用相似条件替代，完全无法替代时才返回 error

## 相似条件替代（优先替代，实在无法替代才报错）

对于不支持的数据维度，选择语义最接近的可用原子替代。仅当输入**完全无法**用任何支持原子近似时才输出 error。

### 替代映射
- RSI/KDJ/威廉等超买超卖指标 → macd_divergence（同类动量反转信号）
- BOLL/布林带 → price_vs_level（布林带本质是价格相对均线的位置）
- 市场情绪/舆情 → market_sentiment 或 market_breadth（用市场整体状态替代个股情绪）

### 替代原则
1. 尽量保留原始逻辑结构（AND/OR）和时间约束
2. 尽量保留原始标的、方向语义、阈值
3. 相似替代不是生搬硬套——无法合理近似时，输出 error

**注意**：不要在条件树里添加任何不属于给定原子列表的原子名称。
不明输入不要返回 error，而应该主动解析、补全并输出条件树。

## 短输入自主增强规则

短输入（20字以内或仅含关键词）很常见，不应拒绝。按以下规则主动补全：

1. **仅含股票名**（如「茅台」）→ 默认行情触发类条件，使用 price_move，默认方向 up，默认阈值 2%
2. **仅含方向但无阈值**（如「茅台大涨」）→ price_move direction=up 默认 pct=3%；「暴跌」默认 direction=down pct=5%
3. **仅含价格位**（如「茅台2000元」）→ price_vs_level relation=above 默认 threshold=price_above
4. **仅含时间**（如「三天后」）→ 补充默认行情触发条件（price_move direction=up pct=2%）作为伴随条件
5. **仅含板块名**（如「半导体板块」）→ 使用 sector_move，默认 direction=up pct=3%
6. **不明含义的短词**（如「关注」「看看」）→ 转为最简单的行情触发（price_move direction=up pct=2%），不拒绝

## 默认参数规则

条件描述缺参数时，按以下默认值补全（不要因为缺参数而报错）：

- **只有幅度没有方向**（如「茅台5%」）→ 默认 direction=up
- **均线金叉/死叉缺少周期** → 默认 fast_period=MA5, slow_period=MA20
- **MACD 缺少方向** → 默认 direction=golden
- **突破/跌破缺少价位** → 默认使用 MA20 均线作为参照（atom=price_vs_ma, period=MA20）
- **成交量放大/缩小缺少倍数** → 默认 multiplier=2（放大）或 multiplier=0.5（缩小）
- **缺少 ticker** → 从上下文中合理推断当前提及的主要股票，无法推断时默认「上证指数」
- **缺少时间约束** → 不添加时间限制（立即开始评估）

## 条件树结构

条件树由两种节点组成：

### 1. 逻辑组节点
```json
{{
  "logic": "AND" | "OR",
  "children": [子节点数组]
}}
```

### 2. 原子叶节点
```json
{{
  "atom": "原子名称",
  "params": {{
    "参数名": 参数值
  }}
}}
```

## 完整示例

**输入条件**: "三天后茅台突破2000元，且（五粮液MA5金叉MA20 或 宁德时代跌超5%）"

**输出**:
```json
{{
  "logic": "AND",
  "children": [
    {{
      "atom": "time_after",
      "params": {{"days": 3}}
    }},
    {{
      "atom": "price_vs_level",
      "params": {{
        "ticker": "贵州茅台",
        "level": 2000,
        "relation": "above"
      }}
    }},
    {{
      "logic": "OR",
      "children": [
        {{
          "atom": "ma_cross",
          "params": {{
            "ticker": "五粮液",
            "fast_period": "MA5",
            "slow_period": "MA20",
            "direction": "golden"
          }}
        }},
        {{
          "atom": "price_move",
          "params": {{
            "ticker": "宁德时代",
            "pct": 5,
            "direction": "down"
          }}
        }}
      ]
    }}
  ]
}}
```

## 可用触发原子（24个）

{atom_list}

## 支持的标的

**A股个股**: 使用中文名称
- 示例："贵州茅台"、"宁德时代"、"平安银行"、"比亚迪"

**A股指数**: 使用中文名称
- 上证指数、深证成指、创业板指、科创50
- 沪深300、中证500、中证1000

**⚠️ 重要**: ticker 参数必须使用中文名称，不要使用代码（如 600519.SH）

## 编译规则

1. **增强优先**: 对模糊、简短、缺参数的输入主动补全，使用上方默认规则；不要猜测时才返回 error
2. **逻辑正确**: AND/OR 逻辑符合用户意图
3. **参数规范**:
   - 数值类型不加引号: `"pct": 5` ✅, `"pct": "5"` ❌
   - 枚举值必须从原子定义中选择
4. **时间条件**: 使用 time_after/time_window/time_before 原子
5. **嵌套**: 支持任意深度嵌套
6. **不要过度拒绝**: 短输入、模糊输入、缺参数的输入都是合法的，应该尽可能编译出条件树

## 常见模式映射

| 自然语言 | 原子 | 说明 |
|---------|------|------|
| "茅台涨5%" | price_move | direction=up, pct=5 |
| "茅台最近3天涨10%" | price_move | direction=up, pct=10, lookback_days=3 |
| "茅台从3月1日涨30%" | price_move | direction=up, pct=30, base_date="2024-03-01" |
| "茅台止盈30%" | price_move | direction=up, pct=30, base_date="2024-03-15" ※止盈止损必须使用base_date，不可使用lookback_days |
| "茅台突破2000元" | price_vs_level | level=2000, relation=above |
| "茅台MA5金叉MA20" | ma_cross | fast_period=MA5, slow_period=MA20, direction=golden |
| "茅台MACD金叉" | macd_cross | direction=golden |
| "茅台成交量放大2倍" | volume_ratio | multiplier=2, relation=above |
| "茅台换手率超5%" | turnover_active | pct=5, relation=above |
| "新能源板块涨3%" | sector_move | sector=新能源, pct=3, direction=up |
| "三天后" | time_after | days=3 |
| "茅台" (仅股票名) | price_move | ticker=贵州茅台, direction=up, pct=2 *(短输入默认补全)* |
| "大涨" (仅方向) | price_move | direction=up, pct=3 *(短输入默认补全)* |
| "半导体" (仅板块) | sector_move | sector=半导体, direction=up, pct=3 *(短输入默认补全)* |

### 特别说明：止盈止损场景
对于"止盈X%"或"止损X%"：
- **必须**使用 `base_date` 参数指定买入日期作为基准，**不可**使用 `lookback_days`
- `base_date` 格式：`"2024-03-15"` (YYYY-MM-DD)，参考对话中提供的"当前时间"作为今天
- 如果用户未明确指定买入日期，将 `base_date` 设为"当前时间"对应的日期（即今天）
- 计算逻辑：(当前价格 - 买入日收盘价) / 买入日收盘价 >= X%

## 输出格式
- 严格输出JSON，不要附带任何解释文字
- 条件描述正常时，输出完整的条件树JSON
- 仅当输入完全无法用任何可用原子近似时才输出: `{{"error": "具体原因"}}`
- 短输入、模糊输入、缺参数输入都是合法输入，不应报 error"""

# ── 动作解析 prompt ──────────────────────────

ACTION_PARSER_PROMPT = """你是一个交易动作解析器。将用户描述的触发后动作转换为结构化输出。

## 支持的动作类型

### 1. buy - 买入/开仓
**触发词**: 买入、开仓、建仓、入手、入场、做多、加仓

**输出格式**:
```json
{
  "action_type": "buy",
  "action_params": {
    "ticker": "贵州茅台",
    "operation_type": "buy",
    "quantity": 100,
    "price": 1800.0,
    "rationale": "MACD金叉信号"
  }
}
```

**参数说明**:
- `ticker` (必填): 股票中文名称，如"贵州茅台"
- `quantity` (选填): 买入数量（股）
- `price` (选填): 目标价格
- `rationale` (选填): 买入理由

### 2. sell - 卖出/平仓
**触发词**: 卖出、平仓、止损、止盈、清仓、离场、做空、减仓

**输出格式**:
```json
{
  "action_type": "sell",
  "action_params": {
    "ticker": "贵州茅台",
    "close_reason": "MACD死叉止损"
  }
}
```

**参数说明**:
- `ticker` (必填): 股票中文名称，如"贵州茅台"
- `close_reason` (必填): 卖出原因

### 3. deep_analysis - 深度分析 (默认)
**触发词**: 分析、研究、评估、review、再看看、提醒我、通知我

**输出格式**:
```json
{
  "action_type": "deep_analysis",
  "action_params": {}
}
```

## 解析规则

### 规则 1: 提取股票名称
- 从动作描述中提取股票中文名称

### 规则 2: 提取数量和价格
- 数量: "100股"、"1000股" → `quantity: 100` / `quantity: 1000`
- 价格: "1800元买入"、"价格2000" → `price: 1800` / `price: 2000`
- 都是可选参数，无法提取时省略

### 规则 3: 默认动作
- 如果动作描述模糊或空白，默认为 `deep_analysis`
- 示例: "提醒我" → deep_analysis

### 规则 4: 买卖判断
- 明确包含买入词汇 → `buy`
- 明确包含卖出词汇 → `sell`
- 都不包含或两者都有 → `deep_analysis`

## 示例

| 输入 | 动作类型 | 说明 |
|------|---------|------|
| "1800元买入茅台" | buy | ticker=茅台, price=1800 |
| "卖出止损" | sell | close_reason="止损" |
| "提醒我" | deep_analysis | 默认动作 |
| "重新分析" | deep_analysis | 明确分析 |
| "" (空) | deep_analysis | 默认动作 |

## 输出格式
严格输出JSON，不要附加其他文字。"""


COMBINED_CORRECTION_PROMPT = """条件树编译结果存在问题，需要修正。请根据以下反馈修正后输出完整的条件树JSON。

## 发现的问题

### 结构问题
{structure_errors}

### 板块名称问题
{sector_issues}

### 原子名称问题
{atom_issues}

### 股票/指数名称问题
{ticker_issues}

## 可用原子列表
{atom_list}

## 修正要求

1. **结构修正**:
   - 确保每个逻辑节点有 "logic" 和 "children"
   - 确保每个原子叶有 "atom" 和 "params"
   - logic 只能是 "AND" 或 "OR"
   - children 必须是非空数组

2. **板块名修正**:
   - 使用候选列表中的精确名称
   - 如果有多个候选，选择最匹配的
   - 如果显示 "__NO_MATCH__"，说明该板块名无法识别

3. **原子名修正**:
   - 使用候选列表中的正确原子名
   - 确保原子名在可用原子列表中
   - 注意原子名的拼写和大小写

4. **股票名修正**:
   - 对于多个匹配，选择第一个（最常见的）
   - 确保使用完整的中文名称
   - 如果显示 "__NO_MATCH__"，说明该股票名无法识别

## 输出格式

只输出修正后的完整条件树JSON，不要附加任何解释文字。

示例输出:
```json
{{
  "logic": "AND",
  "children": [
    {{"atom": "price_move", "params": {{"ticker": "贵州茅台", "direction": "up", "pct": 5}}}},
    {{"atom": "macd_cross", "params": {{"ticker": "贵州茅台", "direction": "golden"}}}}
  ]
}}
```"""

NAME_CORRECTION_PROMPT = """条件树中的一些名称需要修正。你只需输出名称映射，程序会负责替换。不要输出条件树本身。

## 需要修正的名称

### 板块名称
{sector_issues}

### 原子名称
{atom_issues}

### 标的名称
{ticker_issues}

## 规则

1. 从候选列表中选择最匹配的名称
2. 如果只有一个候选，直接使用它
3. 如果有多个候选，选择语义最接近的
4. 如果标记为"__NO_MATCH__"或"完全未识别"，不要包含在输出中（无法修正）
5. 只输出你确定正确的映射，不确定的不要输出

## 输出格式

严格输出以下JSON格式，不要附加任何解释文字：

```json
{{
  "sector": {{"原始名": "修正名"}},
  "atom": {{"原始名": "修正名"}},
  "ticker": {{"原始名": "修正名"}}
}}
```

修正名必须是候选列表中的完整名称。如果某个类别没有需要修正的项，输出空对象。
只输出JSON，不要附加其他文字。"""

STRUCTURE_CORRECTION_PROMPT = """条件树的结构存在语法错误，需要修正。只需修正结构问题，不要改变任何名称。

## 结构问题
{structure_errors}

## 当前条件树
```json
{current_tree}
```

## 可用原子列表
{atom_list}

## 修正要求

1. 确保每个逻辑节点有 "logic" 和 "children"
2. 确保每个原子叶有 "atom" 和 "params"
3. logic 只能是 "AND" 或 "OR"
4. children 必须是非空数组
5. **不要修改任何原子的名称、参数值**——只修正结构

## 输出格式

只输出修正结构后的完整条件树JSON，不要附加任何解释文字。"""


_MAX_CORRECTION_RETRIES = 3


def _validate_tree(tree: dict) -> list[str]:
    """递归校验编译结果的基本 schema 完整性。

    不仅检查根节点，也递归检查所有 children。
    """
    errors: list[str] = []

    def _walk(node: dict, path: str) -> None:
        if "atom" in node:
            if node["atom"] not in ATOM_DEFINITIONS:
                names = list(ATOM_DEFINITIONS.keys())
                errors.append(f"{path}: 未知原子 '{node['atom']}'，可用: {names[:10]}...")
        elif "logic" in node:
            if node["logic"] not in ("AND", "OR"):
                errors.append(f"{path}: 未知 logic '{node['logic']}'，应为 AND/OR")
            if "children" not in node:
                errors.append(f"{path}: 缺少 'children'")
                return
            children = node.get("children")
            if not isinstance(children, list):
                errors.append(f"{path}: 'children' 不是列表")
            elif len(children) == 0:
                errors.append(f"{path}: 'children' 为空")
            else:
                for i, child in enumerate(children):
                    if not isinstance(child, dict):
                        errors.append(f"{path}.children[{i}]: 非字典节点")
                    else:
                        _walk(child, f"{path}.children[{i}]")
        else:
            errors.append(f"{path}: 缺少 'logic' 或 'atom'")

    _walk(tree, "root")
    return errors


def _resolve_reference_time(market: MarketDataProvider, now: datetime | None = None) -> datetime:
    """Resolve compiler reference time from explicit arg, market clock, or wall clock."""
    if now is not None:
        return now
    clock = getattr(market, "clock", None) if market is not None else None
    clock_now = getattr(clock, "now", None)
    if callable(clock_now):
        resolved = clock_now()
        if resolved is not None:
            return resolved
    if clock_now is not None:
        return clock_now
    return datetime.now(BEIJING_TZ)


class TriggerCompiler:
    """两模型串行：A 编译 → 统一校验 → B 评审 → A 修改，再加动作解析"""

    def __init__(self, market: MarketDataProvider | None = None) -> None:
        self._market = market
        self._llm = LLMProvider(
            "deepseek",
            settings.deepseek_model,
            settings.deepseek_api_key,
            settings.deepseek_base_url,
            extra_body={"thinking": {"type": "enabled"}},
        )

    async def compile(
        self,
        name: str,
        condition_nl: str,
        action_nl: str,
        source_task_id: str | None = None,
        source_analysis_id: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        obs_input = {
            "name": name,
            "condition_nl": condition_nl,
            "action_nl": action_nl,
        }
        with start_observation(
            name="trigger_compiler",
            as_type="chain",
            input=safe_observation_value(obs_input),
            metadata={
                "model": settings.deepseek_model,
                "source_task_id": source_task_id or "",
                "source_analysis_id": source_analysis_id or "",
            },
        ) as obs:
            ref_time = _resolve_reference_time(self._market, now)
            # 防止动作信息过短，导致信息提取失败
            if action_nl.strip() and len(action_nl) < 20:
                action_nl = condition_nl + "\n" + action_nl

            condition_tree = await self._compile_condition(condition_nl, ref_time)

            if "error" in condition_tree:
                if obs is not None:
                    obs.update(output=safe_observation_value(condition_tree))
                return condition_tree

            # 先做 schema 规范化（含必填参数校验、类型转换、默认值填充）
            # 必须在时间提取之前，避免 LLM 产生畸形时间原子参数被静默忽略
            condition_tree, validation_errors = normalize_and_validate_tree(condition_tree)
            if validation_errors:
                logger.warning("条件树规范化发现问题: {}", validation_errors)
                # 规范化失败说明条件树结构有问题，返回错误
                if any("未知原子" in e or "缺少必填" in e for e in validation_errors):
                    return {"error": f"条件树规范化失败: {'; '.join(validation_errors)}"}

            not_before, not_after = _extract_time_window(condition_tree, ref_time)

            # 从条件树中移除时间原子（已提取为生命周期字段）
            condition_tree = _strip_time_atoms(condition_tree)

            # 预标注路径，避免 engine 每轮评估都 deep copy
            condition_tree = _annotate_tree_paths(condition_tree)

            action = await self._parse_action(action_nl, ref_time)

            # 若 buy/sell 动作缺少 ticker，从条件树中回退提取
            _ensure_action_ticker(action, condition_tree)

            # 将 action_params 中的名称转为 ticker 代码
            action = _convert_action_names_to_codes(action, self._market)

            # 检查修正循环遗留的警告
            correction_warnings = condition_tree.pop("_correction_warnings", [])

            result = {
                "name": name,
                "condition": condition_tree,
                "action_type": action["action_type"],
                "action_params": action.get("action_params", {}),
                "not_before": not_before,
                "not_after": not_after,
                "warnings": correction_warnings if correction_warnings else None,
            }

            if obs is not None:
                obs.update(output=safe_observation_value(result))
            return result

    async def _compile_condition(self, nl: str, now: datetime) -> dict:
        """A 编译 → 统一校验 → 名称转代码"""
        atom_list = self._format_atoms()
        prompt = COMPILER_A_PROMPT.format(atom_list=atom_list)
        time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        messages_a = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"当前时间：{time_str}"},
            {"role": "user", "content": f"请编译以下触发条件：\n{nl}"},
        ]
        try:
            result = await self._llm.chat_json(messages_a)
        except Exception as e:
            logger.warning("条件编译 LLM 调用失败 (Phase A): {}", e)
            return {"error": f"LLM 调用失败: {e}"}

        # 硬限制命中：直接返回 error，跳过后续校验/评审/修改
        if "error" in result:
            logger.warning("条件编译被拒绝: {}", result.get("error"))
            return result

        # 统一校验：结构 + 板块 + 原子 + 股票名（一次 LLM 调用）
        result = await self._validate_and_correct_tree(result, messages_a, atom_list)
        if "error" in result:
            return result

        # 编译完成，将名称转换为 ticker 代码
        result, unresolved = _convert_names_to_codes(result, self._market)
        if unresolved:
            logger.error("编译结果中存在无法解析的标的名称: {}", unresolved)
            # 返回错误而非静默存储，避免引擎轮询时查无此标的
            return {"error": f"标的名无法解析: {'、'.join(unresolved)}"}

        return result

    async def _validate_and_correct_tree(self, tree: dict, messages_a: list, atom_list: str) -> dict:
        """校验并修正条件树。名称修正走 LLM 产出映射 JSON（程序替换），结构修正走 LLM 产出完整树。

        两阶段分离：
        1. 名称修正：LLM 仅返回 {{sector/atom/ticker: {原始→修正}}} 映射，程序执行替换
        2. 结构修正：LLM 返回修正后的完整条件树（仅当名称修正后仍有结构问题时触发）

        最多重试 _MAX_CORRECTION_RETRIES 次。
        """
        for attempt in range(_MAX_CORRECTION_RETRIES):
            structure_errors = _validate_tree(tree)
            sector_corrections = _find_sector_corrections(tree)
            atom_corrections = _find_atom_corrections(tree)
            ticker_corrections = _find_ticker_name_corrections(tree, self._market)

            if not structure_errors and not sector_corrections and not atom_corrections and not ticker_corrections:
                return tree

            # 构建可读的问题详情
            issue_parts = []
            if structure_errors:
                issue_parts.append(f"结构: {'; '.join(structure_errors)}")
            if sector_corrections:
                detail = ", ".join(
                    f"'{k}' 不存在(候选: {', '.join(repr(c) for c in v)})"
                    for k, v in sector_corrections.items()
                )
                issue_parts.append(f"板块: {detail}")
            if atom_corrections:
                detail = ", ".join(
                    f"'{k}' 不存在(候选: {', '.join(repr(c) for c in v)})"
                    for k, v in atom_corrections.items()
                )
                issue_parts.append(f"原子: {detail}")
            if ticker_corrections:
                detail = ", ".join(
                    f"'{k}' 未识别(候选: {', '.join(repr(c) for c in v)})"
                    for k, v in ticker_corrections.items()
                )
                issue_parts.append(f"标的: {detail}")

            logger.warning(
                "条件树校验发现问题 (第{}次修正): {}",
                attempt + 1,
                "; ".join(issue_parts),
            )

            tree_changed = False

            # ── 阶段1：名称修正 → LLM 产出映射 JSON → 程序替换 ──
            if sector_corrections or atom_corrections or ticker_corrections:
                si_parts = []
                for original, candidates in sector_corrections.items():
                    cand_str = "、".join(f'"{c}"' for c in candidates)
                    si_parts.append(f'  "{original}" 未找到，候选: {cand_str}')
                si = "\n".join(si_parts) if si_parts else "无"

                ai_parts = []
                for original, candidates in atom_corrections.items():
                    cand_str = "、".join(f'"{c}"' for c in candidates)
                    ai_parts.append(f'  "{original}" 未找到，候选: {cand_str}')
                ai = "\n".join(ai_parts) if ai_parts else "无"

                ti_parts = []
                for original, candidates in ticker_corrections.items():
                    if candidates == ["__NO_MATCH__"]:
                        ti_parts.append(f'  "{original}" 完全未识别，请确认名称是否正确')
                    else:
                        cand_str = "、".join(f'"{c}"' for c in candidates)
                        ti_parts.append(f'  "{original}" 匹配到多个结果: {cand_str}，请指定更精确的名称')
                ti = "\n".join(ti_parts) if ti_parts else "无"

                name_prompt = NAME_CORRECTION_PROMPT.format(
                    sector_issues=si,
                    atom_issues=ai,
                    ticker_issues=ti,
                )

                try:
                    mapping = await self._llm.chat_json([
                        {"role": "system", "content": "你是一个交易条件名称修正器。只输出名称映射JSON，不要输出其他内容。"},
                        {"role": "user", "content": name_prompt},
                    ])
                except Exception as e:
                    logger.warning("名称修正 LLM 调用失败 (第{}次): {}", attempt + 1, e)
                    continue

                # 程序化替换
                if isinstance(mapping, dict):
                    sm = mapping.get("sector", {})
                    am = mapping.get("atom", {})
                    tm = mapping.get("ticker", {})
                    if sm and isinstance(sm, dict):
                        _apply_sector_mapping(tree, sm)
                        logger.info("LLM 名称修正(板块): {}", sm)
                        tree_changed = True
                    if am and isinstance(am, dict):
                        _apply_atom_mapping(tree, am)
                        logger.info("LLM 名称修正(原子): {}", am)
                        tree_changed = True
                    if tm and isinstance(tm, dict):
                        _apply_ticker_mapping(tree, tm)
                        logger.info("LLM 名称修正(标的): {}", tm)
                        tree_changed = True

                # 名称修正后重新校验
                structure_errors = _validate_tree(tree)
                sector_corrections = _find_sector_corrections(tree)
                atom_corrections = _find_atom_corrections(tree)
                ticker_corrections = _find_ticker_name_corrections(tree, self._market)

                # 名称问题已全部解决 → 直接返回
                if not structure_errors and not sector_corrections and not atom_corrections and not ticker_corrections:
                    return tree

            # ── 阶段2：结构修正 → LLM 产出完整树（仅当还有结构错误时）──
            if structure_errors:
                se = "; ".join(structure_errors)
                struct_prompt = STRUCTURE_CORRECTION_PROMPT.format(
                    structure_errors=se,
                    current_tree=json.dumps(tree, ensure_ascii=False),
                    atom_list=atom_list,
                )

                try:
                    # 独立消息，不污染主编译上下文
                    tree = await self._llm.chat_json([
                        {"role": "system", "content": "你是一个JSON结构修正器。只修正语法结构错误，不要改变任何名称。只输出修正后的完整条件树JSON。"},
                        {"role": "user", "content": struct_prompt},
                    ])
                    tree_changed = True
                except Exception as e:
                    if _is_transient_tool_error(e):
                        logger.warning("结构修正 LLM 瞬态错误 (第{}次，将重试): {}", attempt + 1, e)
                        continue
                    logger.warning("结构修正 LLM 调用失败 (第{}次): {}", attempt + 1, e)
                    return {"error": f"条件树结构修正失败: {e}"}

            # 本轮未做任何有效修改 → LLM 返回的结果中没有可用的修正
            if not tree_changed:
                remaining_parts = []
                if structure_errors:
                    remaining_parts.append(f"结构: {'; '.join(structure_errors)}")
                if sector_corrections:
                    detail = ", ".join(f"'{k}'" for k in sector_corrections)
                    remaining_parts.append(f"板块: {detail}")
                if atom_corrections:
                    detail = ", ".join(f"'{k}'" for k in atom_corrections)
                    remaining_parts.append(f"原子: {detail}")
                if ticker_corrections:
                    detail = ", ".join(f"'{k}'" for k in ticker_corrections)
                    remaining_parts.append(f"标的: {detail}")
                logger.error(
                    "条件树修正失败 (第{}次): LLM未返回可用修正，仍存在的问题 → {}",
                    attempt + 1,
                    "; ".join(remaining_parts),
                )
                error_parts = []
                if structure_errors:
                    error_parts.extend(structure_errors)
                return {"error": f"条件树无法修正: {'; '.join(error_parts)}"}

        # 超过重试次数，最终检查
        structure_errors = _validate_tree(tree)
        atom_corrections = _find_atom_corrections(tree)
        sector_corrections = _find_sector_corrections(tree)
        ticker_corrections = _find_ticker_name_corrections(tree, self._market)

        if structure_errors or atom_corrections:
            logger.error(
                "条件树经过 {} 次修正仍存在严重错误（结构/原子问题），拒绝编译",
                _MAX_CORRECTION_RETRIES,
            )
            error_parts = []
            if structure_errors:
                error_parts.extend(structure_errors)
            if atom_corrections:
                error_parts.extend([f"未知原子: {k}" for k in atom_corrections])
            return {"error": f"条件树无法修正: {'; '.join(error_parts)}"}

        if sector_corrections or ticker_corrections:
            logger.warning(
                "条件树经过 {} 次修正后仍有板块/标的名问题，已附加警告",
                _MAX_CORRECTION_RETRIES,
            )
            tree["_correction_warnings"] = []
            if sector_corrections:
                tree["_correction_warnings"].extend(
                    [f"板块名不匹配: {k} -> {v}" for k, v in sector_corrections.items()]
                )
            if ticker_corrections:
                tree["_correction_warnings"].extend(
                    [f"标的名未识别: {k}" for k in ticker_corrections]
                )

        return tree

    async def _parse_action(self, action_nl: str, now: datetime | None = None) -> dict:
        """单 LLM 调用解析动作 NL，含校验"""
        if not action_nl.strip():
            return {"action_type": "deep_analysis", "action_params": {}}

        messages = [
            {"role": "system", "content": ACTION_PARSER_PROMPT},
        ]
        if now is not None:
            time_str = now.strftime("%Y-%m-%d %H:%M:%S")
            messages.append({"role": "user", "content": f"当前时间：{time_str}"})
        messages.append({"role": "user", "content": f"请解析以下动作描述：\n{action_nl}"})
        try:
            result = await self._llm.chat_json(messages)
            action_type = result.get("action_type", "")
            if action_type == "trade":
                result["action_type"] = "sell"
                action_type = "sell"
            valid_types = {"buy", "sell", "deep_analysis"}

            if action_type not in valid_types:
                # 不静默回退——记录原始 NL 并报错
                logger.warning(
                    "未知的 action_type '{}'，原描述: {}，已回退为 deep_analysis",
                    action_type,
                    action_nl[:100],
                )
                return {"action_type": "deep_analysis", "action_params": {"original_description": action_nl[:200]}}

            return result
        except Exception as e:
            logger.warning("动作解析失败，已回退为 deep_analysis: {}", e)
            return {"action_type": "deep_analysis", "action_params": {"original_description": action_nl[:200]}}

    def _format_atoms(self) -> str:
        return format_atom_list_for_prompt()
