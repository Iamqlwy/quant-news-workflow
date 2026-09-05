"""触发原子 Schema (v3) —— 强类型定义 + 校验 + 条件树评估

设计原则：
1. 原子描述"可观测的市场事实"，不含分析判断
2. Schema 是唯一真相源：compiler / engine / evaluator 均从此派生
3. 时间约束不在原子中——时间由 compiler 提取为 trigger 生命周期字段
"""

from __future__ import annotations

from typing import Any

# ═══════════════════════════════════════════════
# 原子 Schema (22 atoms, 5 类)
# ═══════════════════════════════════════════════

ATOM_SCHEMA: dict[str, dict[str, Any]] = {
    # === 价格状态 (5) ===
    "price_move": {
        "description": "价格变动：涨了/跌了X%（可指定回溯天数或基准日期）",
        "required_params": {
            "ticker": "str",
            "direction": ["up", "down"],
            "pct": "number",
        },
        "optional_params": {
            "lookback_days": (1, "int"),
            "base_date": (None, "str"),
        },
        "ticker_data_needs": ["price", "history"],
    },
    "price_vs_level": {
        "description": "价格与参考位的关系：突破/跌破/接近某价位或均线",
        "required_params": {
            "ticker": "str",
            "level": "str_or_number",  # 数值 或 MA5/MA10/MA20/MA60
            "relation": ["above", "below", "near"],
        },
        "optional_params": {
            "tolerance_pct": (1, "number"),
        },
        "ticker_data_needs": ["history"],
    },
    "new_extreme": {
        "description": "创N日新高或新低",
        "required_params": {
            "ticker": "str",
            "direction": ["high", "low"],
            "n_days": "int",
        },
        "optional_params": {},
        "ticker_data_needs": ["history", "price"],
    },
    "gap": {
        "description": "跳空：今日开盘相对昨日收盘的跳空幅度",
        "required_params": {
            "ticker": "str",
            "direction": ["up", "down"],
            "min_pct": "number",
        },
        "optional_params": {},
        "ticker_data_needs": ["price", "history"],
    },
    "consecutive_move": {
        "description": "连续N天涨/跌",
        "required_params": {
            "ticker": "str",
            "direction": ["up", "down"],
            "n_days": "int",
        },
        "optional_params": {},
        "ticker_data_needs": ["history"],
    },
    # === 量价关系 (3) ===
    "volume_ratio": {
        "description": "当日成交量相对N日均量的倍数（放量/缩量）",
        "required_params": {
            "ticker": "str",
            "multiplier": "number",
            "relation": ["above", "below"],
        },
        "optional_params": {
            "n_days": (20, "int"),
        },
        "ticker_data_needs": ["history"],
    },
    "turnover_active": {
        "description": "换手率活跃度",
        "required_params": {
            "ticker": "str",
            "pct": "number",
            "relation": ["above", "below"],
        },
        "optional_params": {},
        "ticker_data_needs": ["turnover"],
    },
    "amplitude_wide": {
        "description": "日内振幅（最高最低差相对开盘）",
        "required_params": {
            "ticker": "str",
            "pct": "number",
            "relation": ["above", "below"],
        },
        "optional_params": {},
        "ticker_data_needs": ["snapshot"],
    },
    # === 趋势结构 (3) ===
    "ma_slope": {
        "description": "均线自身方向：拐头向上/向下/走平",
        "required_params": {
            "ticker": "str",
            "period": ["MA5", "MA10", "MA20", "MA60"],
            "direction": ["up", "down", "flat"],
        },
        "optional_params": {},
        "ticker_data_needs": ["history"],
    },
    "ma_cross": {
        "description": "均线金叉/死叉",
        "required_params": {
            "ticker": "str",
            "fast_period": ["MA5", "MA10", "MA20", "MA60"],
            "slow_period": ["MA5", "MA10", "MA20", "MA60"],
            "direction": ["golden", "death"],
        },
        "optional_params": {},
        "ticker_data_needs": ["history"],
    },
    "ma_alignment": {
        "description": "均线排列：多头排列(MA5>MA10>MA20>MA60)或空头排列",
        "required_params": {
            "ticker": "str",
            "pattern": ["bullish", "bearish"],
        },
        "optional_params": {},
        "ticker_data_needs": ["history"],
    },
    "macd_cross": {
        "description": "MACD金叉/死叉：DIF穿越DEA",
        "required_params": {
            "ticker": "str",
            "direction": ["golden", "death"],
        },
        "optional_params": {},
        "ticker_data_needs": ["history"],
    },
    "macd_divergence": {
        "description": "MACD背离：价格与MACD指标背离",
        "required_params": {
            "ticker": "str",
            "pattern": ["bullish", "bearish"],
        },
        "optional_params": {
            "lookback_days": (5, "int"),
        },
        "ticker_data_needs": ["history"],
    },
    # === 日内动态 (3) ===
    "intraday_reversal": {
        "description": "日内反转：冲高回落或探底回升",
        "required_params": {
            "ticker": "str",
            "pattern": ["shot_up_fall", "dip_recover"],
            "move_pct": "number",
        },
        "optional_params": {
            "retrace_ratio": (50, "number"),
        },
        "ticker_data_needs": ["snapshot"],
    },
    "intraday_round_trip": {
        "description": "日内往返：A字(先涨后跌回原点)或V字(先跌后涨回原点)",
        "required_params": {
            "ticker": "str",
            "direction": ["A", "V"],
            "min_move_pct": "number",
        },
        "optional_params": {
            "tolerance_pct": (0.5, "number"),
        },
        "ticker_data_needs": ["snapshot"],
    },
    "intraday_trend": {
        "description": "日内单边走势：持续同向运行",
        "required_params": {
            "ticker": "str",
            "direction": ["up", "down"],
            "minutes": "int",
            "min_pct": "number",
        },
        "optional_params": {},
        "ticker_data_needs": ["snapshot"],
    },
    # === 板块与市场 (5) ===
    "sector_move": {
        "description": "板块涨跌，可选异动检测（N分钟内涨速）",
        "required_params": {
            "sector": "str",
            "direction": ["up", "down"],
            "pct": "number",
        },
        "optional_params": {
            "velocity_minutes": (None, "int"),
        },
        "ticker_data_needs": [],
        "sector_data_needs": ["overview"],
    },
    "sector_breadth": {
        "description": "板块内涨跌家数比（涨多跌少）",
        "required_params": {
            "sector": "str",
            "up_ratio_min": "number",
        },
        "optional_params": {},
        "ticker_data_needs": [],
        "sector_data_needs": ["overview"],
    },
    "sector_limit_ratio": {
        "description": "板块涨停/跌停家数",
        "required_params": {
            "sector": "str",
            "direction": ["up", "down"],
            "min_count": "int",
        },
        "optional_params": {},
        "ticker_data_needs": [],
        "sector_data_needs": ["overview", "members"],
        "member_ticker_needs": ["zdt_record"],
    },
    "market_breadth": {
        "description": "全市场涨跌比（含平均涨幅）",
        "required_params": {
            "up_down_ratio_min": "number",
        },
        "optional_params": {
            "avg_pct_min": (None, "number"),
        },
        "ticker_data_needs": [],
        "sector_data_needs": [],
    },
    "market_sentiment": {
        "description": "市场情绪综合评分(0-100)，基于涨跌广度/指数协同/平均涨幅/量能/昨日涨停今日表现",
        "required_params": {
            "min_score": "number",
        },
        "optional_params": {
            "direction": ("bullish", ["bullish", "bearish"]),
        },
        "ticker_data_needs": [],
        "sector_data_needs": [],
    },
    # === 时间 (3, meta: compiler 提取后从条件树中移除，不进入 evaluator) ===
    "time_after": {
        "description": "距创建后N天起触发（meta原子，不在条件树中评估）",
        "required_params": {"days": "int"},
        "optional_params": {},
        "ticker_data_needs": [],
        "sector_data_needs": [],
        "meta": True,
    },
    "time_window": {
        "description": "距创建后N到M天内有效（meta原子）",
        "required_params": {"days_min": "int", "days_max": "int"},
        "optional_params": {},
        "ticker_data_needs": [],
        "sector_data_needs": [],
        "meta": True,
    },
    "time_before": {
        "description": "距创建后N天内有效，超时失效（meta原子）",
        "required_params": {"days": "int"},
        "optional_params": {},
        "ticker_data_needs": [],
        "sector_data_needs": [],
        "meta": True,
    },
}


# ═══════════════════════════════════════════════
# 从 Schema 派生的数据需求表（供 engine 使用）
# ═══════════════════════════════════════════════


def build_ticker_data_keys() -> dict[str, set[str]]:
    """从 ATOM_SCHEMA 生成 _ATOM_TICKER_KEYS"""
    result: dict[str, set[str]] = {}
    for name, schema in ATOM_SCHEMA.items():
        needs = set(schema.get("ticker_data_needs", []))
        result[name] = needs
    return result


def build_sector_data_keys() -> dict[str, set[str]]:
    """从 ATOM_SCHEMA 生成 _ATOM_SECTOR_KEYS"""
    result: dict[str, set[str]] = {}
    for name, schema in ATOM_SCHEMA.items():
        needs = set(schema.get("sector_data_needs", []))
        if needs:
            result[name] = needs
    return result


def build_member_ticker_keys() -> dict[str, set[str]]:
    """从 ATOM_SCHEMA 生成 _MEMBER_NEED_KEYS"""
    result: dict[str, set[str]] = {}
    for name, schema in ATOM_SCHEMA.items():
        needs = set(schema.get("member_ticker_needs", []))
        if needs:
            result[name] = needs
    return result


# ═══════════════════════════════════════════════
# 条件树校验与规范化
# ═══════════════════════════════════════════════


def _coerce_value(value: Any, type_spec: str | list) -> tuple[Any, str | None]:
    """尝试将 value 转换为 type_spec 要求的类型。

    Returns (coerced_value, error_or_None)
    """
    if isinstance(type_spec, list):
        # enum
        val_str = str(value).strip().lower()
        valid = [v.lower() for v in type_spec]
        if val_str in valid:
            idx = valid.index(val_str)
            return type_spec[idx], None
        return value, f"枚举值 '{value}' 不在 {type_spec} 中"
    elif type_spec == "str":
        return str(value), None
    elif type_spec == "str_or_number":
        # price_vs_level 的 level 字段
        if isinstance(value, (int, float)):
            return value, None
        s = str(value).strip().upper()
        if s in ("MA5", "MA10", "MA20", "MA60"):
            return s, None
        try:
            return float(value), None
        except (ValueError, TypeError):
            return value, f"'{value}' 不是合法的数值或均线(MA5/MA10/MA20/MA60)"
    elif type_spec == "number":
        try:
            v = float(value)
            return v if v != int(v) else int(v), None
        except (ValueError, TypeError):
            return value, f"'{value}' 无法转为数字"
    elif type_spec == "int":
        try:
            return int(value), None
        except (ValueError, TypeError):
            return value, f"'{value}' 无法转为整数"
    return value, f"未知类型约束: {type_spec}"


def normalize_and_validate_tree(tree: dict) -> tuple[dict, list[str]]:
    """递归校验并规范化条件树。

    对原子叶：
    - 校验 atom 名存在
    - 填充 optional_params 默认值
    - 校验 required_params 存在
    - 枚举值/类型转换
    - 删除未知参数

    对逻辑组：
    - 校验 logic 为 AND/OR
    - 递归处理 children

    Returns (normalized_tree, errors)
    """
    errors: list[str] = []

    def _walk(node: dict, path: str) -> dict:
        if "atom" in node:
            return _normalize_leaf(node, path)
        elif "logic" in node:
            logic = node.get("logic", "AND")
            if logic not in ("AND", "OR"):
                errors.append(f"{path}: 未知 logic '{logic}'，应为 AND/OR")
                logic = "AND"
            children = node.get("children")
            if not isinstance(children, list) or len(children) == 0:
                errors.append(f"{path}: children 为空或非列表")
                return {"logic": logic, "children": []}
            normalized_children = []
            for i, child in enumerate(children):
                if not isinstance(child, dict):
                    errors.append(f"{path}.children[{i}]: 非字典节点")
                    continue
                normalized_children.append(_walk(child, f"{path}.children[{i}]"))
            return {"logic": logic, "children": normalized_children}
        else:
            errors.append(f"{path}: 节点既无 'atom' 也无 'logic'")
            return node

    def _normalize_leaf(node: dict, path: str) -> dict:
        atom_name = node.get("atom", "")
        schema = ATOM_SCHEMA.get(atom_name)

        if schema is None:
            errors.append(f"{path}: 未知原子 '{atom_name}'")
            return node

        params = node.get("params", {})
        if not isinstance(params, dict):
            errors.append(f"{path}: params 不是字典")
            params = {}

        normalized_params: dict[str, Any] = {}

        # 1. 必填参数
        for pname, ptype in schema["required_params"].items():
            if pname not in params:
                errors.append(f"{path}: 缺少必填参数 '{pname}'")
                continue
            val, err = _coerce_value(params[pname], ptype)
            if err:
                errors.append(f"{path}.{pname}: {err}")
            normalized_params[pname] = val

        # 2. 可选参数（填默认值 + 类型转换）
        for pname, (default, ptype) in schema["optional_params"].items():
            if pname in params:
                val, err = _coerce_value(params[pname], ptype)
                if err:
                    errors.append(f"{path}.{pname}: {err}")
                normalized_params[pname] = val
            else:
                # 如果 base_date 被 LLM 显式设置，不填 lookback_days 的默认值
                if pname == "lookback_days" and "base_date" in params:
                    normalized_params[pname] = None
                else:
                    normalized_params[pname] = default

        # 3. 未知参数告警
        known = set(schema["required_params"].keys()) | set(schema["optional_params"].keys())
        for pname in params:
            if pname not in known:
                errors.append(f"{path}: 未知参数 '{pname}'")

        return {"atom": atom_name, "params": normalized_params}

    normalized = _walk(tree, "root")
    return normalized, errors


# ═══════════════════════════════════════════════
# 条件树评估（路径匹配，不污染原树）
# ═══════════════════════════════════════════════


def evaluate_condition_tree(tree: dict[str, Any], atom_results: dict[str, bool]) -> bool:
    """递归评估 AND/OR 条件树。

    atom_results 的 key 是路径（如 "0", "1.2.0"），
    由 engine 的 _collect_atoms 按 children 索引路径分配。
    """
    if "atom" in tree:
        path = tree.get("_path", "")
        return atom_results.get(path, False)

    logic = tree.get("logic", "AND")
    children = tree.get("children", [])
    if not children:
        return False

    if logic == "AND":
        return all(evaluate_condition_tree(c, atom_results) for c in children)
    elif logic == "OR":
        return any(evaluate_condition_tree(c, atom_results) for c in children)
    return False


# ═══════════════════════════════════════════════
# 编译 Prompt 格式化（保持向后兼容的接口）
# ═══════════════════════════════════════════════


def format_atom_list_for_prompt() -> str:
    """格式化原子列表为紧凑字符串，供 compiler prompt 使用"""
    lines = []
    for name, schema in ATOM_SCHEMA.items():
        req = schema["required_params"]
        opt = schema["optional_params"]
        parts = []
        for pname, ptype in req.items():
            if isinstance(ptype, list):
                parts.append(f"{pname}: {'/'.join(ptype)}")
            else:
                parts.append(f"{pname}: {ptype}")
        for pname, (default, ptype) in opt.items():
            if isinstance(ptype, list):
                parts.append(f"[{pname}]: {'/'.join(ptype)}(默认{default})")
            else:
                parts.append(f"[{pname}]: {ptype}(默认{default})")
        param_str = ", ".join(parts)
        lines.append(f"- **{name}**: {schema['description']}  参数: {param_str}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════
# 向后兼容的别名（compiler 旧代码引用 ATOM_DEFINITIONS）
# ═══════════════════════════════════════════════

# 将 ATOM_SCHEMA 转为旧格式的 ATOM_DEFINITIONS（供 compiler._format_atoms 过渡使用）
ATOM_DEFINITIONS: dict[str, dict[str, Any]] = {}
for _name, _schema in ATOM_SCHEMA.items():
    _params_desc: dict[str, str] = {}
    for _pname, _ptype in _schema["required_params"].items():
        if isinstance(_ptype, list):
            _params_desc[_pname] = "/".join(_ptype)
        else:
            _params_desc[_pname] = _ptype
    for _pname, (_default, _ptype) in _schema["optional_params"].items():
        if isinstance(_ptype, list):
            _params_desc[_pname] = "/".join(_ptype) + f"(默认{_default})"
        else:
            _params_desc[_pname] = str(_ptype) + f"(默认{_default})"
    ATOM_DEFINITIONS[_name] = {
        "description": _schema["description"],
        "params": _params_desc,
    }
