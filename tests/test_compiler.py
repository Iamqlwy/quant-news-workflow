"""TriggerCompiler 编译管线测试 —— 覆盖条件编译、动作解析、校验修正全流程。

所有 LLM 调用均被 mock，不依赖外部 API。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.timezone import BEIJING_TZ
from src.triggers.atoms import evaluate_condition_tree
from src.triggers.compiler import (
    TriggerCompiler,
    _annotate_tree_paths,
    _apply_atom_mapping,
    _apply_sector_mapping,
    _collect_atom_names,
    _collect_sector_names,
    _extract_time_window,
    _find_atom_corrections,
    _strip_time_atoms,
    _validate_tree,
)

_NOW = datetime(2026, 6, 3, 14, 30, tzinfo=BEIJING_TZ)

# ── 测试工具 ────────────────────────────────────


def _make_compiler(responses: list[dict]) -> TriggerCompiler:
    """构造一个 chat_json 按序返回 responses 的 TriggerCompiler。"""
    compiler = TriggerCompiler.__new__(TriggerCompiler)
    compiler._llm = Mock()
    compiler._llm.chat_json = AsyncMock(side_effect=list(responses))
    class _MockClock:
        @staticmethod
        def now():
            from datetime import datetime
            return datetime(2026, 6, 10, 10, 30, 0)
    class _MockMarket:
        clock = _MockClock
        @staticmethod
        def resolve_index_name(name): return None
        @staticmethod
        def resolve_stock_ticker(name): return None
    compiler._market = _MockMarket()
    return compiler


# ═══════════════════════════════════════════════════
#  1. _validate_tree  ——  schema 完整性校验
# ═══════════════════════════════════════════════════


class TestValidateTree:
    def test_valid_atom_leaf(self):
        errors = _validate_tree({"atom": "price_move", "params": {"ticker": "600519.SH", "direction": "up", "pct": 5}})
        assert errors == []

    def test_valid_logic_and(self):
        errors = _validate_tree({"logic": "AND", "children": [{"atom": "price_move", "params": {}}]})
        assert errors == []

    def test_valid_logic_or(self):
        errors = _validate_tree({"logic": "OR", "children": [{"atom": "price_move", "params": {}}]})
        assert errors == []

    def test_unknown_atom(self):
        errors = _validate_tree({"atom": "stock_explosion", "params": {}})
        assert any("未知原子" in e for e in errors)

    def test_unknown_logic(self):
        errors = _validate_tree({"logic": "XOR", "children": [{"atom": "price_move", "params": {}}]})
        assert any("未知 logic" in e for e in errors)

    def test_missing_children(self):
        errors = _validate_tree({"logic": "AND"})
        assert any("缺少 'children'" in e for e in errors)

    def test_no_atom_no_logic(self):
        errors = _validate_tree({"foo": "bar"})
        assert any("缺少 'logic' 或 'atom'" in e for e in errors)

    def test_time_atoms_are_valid(self):
        """time_after 在 ATOM_DEFINITIONS 中，应在结构校验时通过"""
        errors = _validate_tree({"atom": "time_after", "params": {"days": 3}})
        assert errors == []


# ═══════════════════════════════════════════════════
#  2. _extract_time_window  ——  时间窗口提取
# ═══════════════════════════════════════════════════


class TestExtractTimeWindow:
    def test_time_after_single(self):
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "time_after", "params": {"days": 3}},
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},
            ],
        }
        not_before, not_after = _extract_time_window(tree, _NOW)
        assert not_before == (_NOW + timedelta(days=3)).isoformat()
        assert not_after is None

    def test_time_before_single(self):
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "time_before", "params": {"days": 7}},
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},
            ],
        }
        not_before, not_after = _extract_time_window(tree, _NOW)
        assert not_before is None
        assert not_after == (_NOW + timedelta(days=7)).isoformat()

    def test_time_window_range(self):
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "time_window", "params": {"days_min": 2, "days_max": 10}},
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},
            ],
        }
        not_before, not_after = _extract_time_window(tree, _NOW)
        assert not_before == (_NOW + timedelta(days=2)).isoformat()
        assert not_after == (_NOW + timedelta(days=10)).isoformat()

    def test_multiple_time_atoms_merged(self):
        """多个 time_after 取最晚，多个 time_before 取最早"""
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "time_after", "params": {"days": 1}},
                {"atom": "time_after", "params": {"days": 5}},
                {"atom": "time_before", "params": {"days": 10}},
                {"atom": "time_before", "params": {"days": 20}},
            ],
        }
        not_before, not_after = _extract_time_window(tree, _NOW)
        assert not_before == (_NOW + timedelta(days=5)).isoformat()
        assert not_after == (_NOW + timedelta(days=10)).isoformat()

    def test_or_branch_ignored(self):
        """OR 分支中的 time_after 不应被提取"""
        tree = {
            "logic": "OR",
            "children": [
                {"atom": "time_after", "params": {"days": 3}},
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},
            ],
        }
        not_before, not_after = _extract_time_window(tree, _NOW)
        assert not_before is None
        assert not_after is None

    def test_nested_or_under_and_ignored(self):
        """AND 的直接子节点是 OR 时，OR 内的时间原子不被提取"""
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "time_after", "params": {"days": 3}},
                {
                    "logic": "OR",
                    "children": [
                        {"atom": "time_before", "params": {"days": 10}},
                        {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},
                    ],
                },
            ],
        }
        not_before, not_after = _extract_time_window(tree, _NOW)
        # time_after 在 AND 直接子节点中，被提取
        assert not_before == (_NOW + timedelta(days=3)).isoformat()
        # time_before 在 OR 内部，不被提取
        assert not_after is None

    def test_bad_days_value_not_crash(self):
        """非数字 days 值不应导致崩溃"""
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "time_after", "params": {"days": "three"}},
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},
            ],
        }
        not_before, not_after = _extract_time_window(tree, _NOW)
        # 应优雅降级，不抛异常
        assert not_before is None
        assert not_after is None

    def test_no_time_atoms(self):
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},
            ],
        }
        not_before, not_after = _extract_time_window(tree, _NOW)
        assert not_before is None
        assert not_after is None

    def test_contradiction_logged_but_not_crash(self):
        """时间矛盾应记录 warning 但不抛异常"""
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "time_after", "params": {"days": 10}},
                {"atom": "time_before", "params": {"days": 5}},
            ],
        }
        not_before, not_after = _extract_time_window(tree, _NOW)
        assert not_before > not_after  # 矛盾但返回值仍可用


# ═══════════════════════════════════════════════════
#  3. _strip_time_atoms  ——  时间原子剥离
# ═══════════════════════════════════════════════════


class TestStripTimeAtoms:
    def test_removes_time_atoms(self):
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "time_after", "params": {"days": 3}},
                {"atom": "time_window", "params": {"days_min": 1, "days_max": 7}},
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},
            ],
        }
        result = _strip_time_atoms(tree)
        assert "atom" in result
        assert result["atom"] == "price_move"

    def test_single_child_promoted(self):
        """只剩一个子节点时提升为根"""
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "time_after", "params": {"days": 3}},
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},
            ],
        }
        result = _strip_time_atoms(tree)
        assert "logic" not in result
        assert result["atom"] == "price_move"

    def test_no_mutation(self):
        """原树不被修改"""
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "time_after", "params": {"days": 3}},
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},
            ],
        }
        original_children_count = len(tree["children"])
        _strip_time_atoms(tree)
        assert len(tree["children"]) == original_children_count

    def test_or_tree_unchanged(self):
        """OR 树不剥离时间原子（不满足 AND 条件）"""
        tree = {
            "logic": "OR",
            "children": [
                {"atom": "time_after", "params": {"days": 3}},
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},
            ],
        }
        result = _strip_time_atoms(tree)
        assert len(result["children"]) == 2


# ═══════════════════════════════════════════════════
#  4. _annotate_tree_paths  ——  路径标注
# ═══════════════════════════════════════════════════


class TestAnnotateTreePaths:
    def test_flat_and_tree(self):
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},
                {"atom": "volume_ratio", "params": {"ticker": "000001.SZ", "multiplier": 2, "relation": "above"}},
            ],
        }
        annotated = _annotate_tree_paths(tree)
        assert annotated["children"][0]["_path"] == "0"
        assert annotated["children"][1]["_path"] == "1"

    def test_nested_tree(self):
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},
                {
                    "logic": "OR",
                    "children": [
                        {"atom": "ma_cross", "params": {"ticker": "000001.SZ", "fast_period": "MA5", "slow_period": "MA20", "direction": "golden"}},
                        {"atom": "volume_ratio", "params": {"ticker": "000001.SZ", "multiplier": 2, "relation": "above"}},
                    ],
                },
            ],
        }
        annotated = _annotate_tree_paths(tree)
        assert annotated["children"][0]["_path"] == "0"
        assert annotated["children"][1]["children"][0]["_path"] == "1.0"
        assert annotated["children"][1]["children"][1]["_path"] == "1.1"

    def test_single_atom_root(self):
        tree = {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}}
        annotated = _annotate_tree_paths(tree)
        assert annotated["_path"] == "0"

    def test_paths_work_with_evaluate_condition_tree(self):
        """标注路径后可直接用于 evaluate_condition_tree"""
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},
                {"atom": "volume_ratio", "params": {"ticker": "000001.SZ", "multiplier": 2, "relation": "above"}},
            ],
        }
        annotated = _annotate_tree_paths(tree)

        # 两个 atom 都触发
        assert evaluate_condition_tree(annotated, {"0": True, "1": True}) is True
        # 只有一个 atom 触发 → AND 不满足
        assert evaluate_condition_tree(annotated, {"0": True, "1": False}) is False
        # 两个都不触发
        assert evaluate_condition_tree(annotated, {"0": False, "1": False}) is False


# ═══════════════════════════════════════════════════
#  5. 板块名 / 原子名 收集 & 修正
# ═══════════════════════════════════════════════════


class TestSectorNameCollection:
    def test_collects_all_sector_atoms(self):
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "sector_move", "params": {"sector": "半导体", "direction": "up", "pct": 3}},
                {"atom": "sector_breadth", "params": {"sector": "新能源", "up_ratio_min": 0.6}},
                {"atom": "sector_limit_ratio", "params": {"sector": "人工智能", "direction": "up", "min_count": 5}},
            ],
        }
        names = _collect_sector_names(tree)
        assert ("半导体", "sector") in names
        assert ("新能源", "sector") in names
        assert ("人工智能", "sector") in names

    def test_collects_sector_a_sector_b(self):
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "sector_move", "params": {"sector": "半导体", "sector_a": "芯片", "sector_b": "元件"}},
            ],
        }
        names = _collect_sector_names(tree)
        assert ("半导体", "sector") in names
        assert ("芯片", "sector_a") in names
        assert ("元件", "sector_b") in names

    def test_empty_tree_returns_empty(self):
        tree = {"logic": "AND", "children": [{"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}}]}
        assert _collect_sector_names(tree) == []


class TestSectorMapping:
    def test_replaces_matching_names(self):
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "sector_move", "params": {"sector": "半导体芯片", "direction": "up", "pct": 3}},
                {"atom": "sector_breadth", "params": {"sector": "新能源车", "up_ratio_min": 0.6}},
            ],
        }
        _apply_sector_mapping(tree, {"半导体芯片": "半导体", "新能源车": "新能源汽车"})
        assert tree["children"][0]["params"]["sector"] == "半导体"
        assert tree["children"][1]["params"]["sector"] == "新能源汽车"

    def test_non_matching_names_unchanged(self):
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "sector_move", "params": {"sector": "半导体", "direction": "up", "pct": 3}},
            ],
        }
        _apply_sector_mapping(tree, {"其他板块": "某某板块"})
        assert tree["children"][0]["params"]["sector"] == "半导体"


class TestAtomNameCollection:
    def test_collects_all_atom_names(self):
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "price_move", "params": {}},
                {"atom": "volume_ratio", "params": {}},
                {
                    "logic": "OR",
                    "children": [
                        {"atom": "ma_cross", "params": {}},
                        {"atom": "gap", "params": {}},
                    ],
                },
            ],
        }
        names = _collect_atom_names(tree)
        assert names == {"price_move", "volume_ratio", "ma_cross", "gap"}

    def test_empty_tree_returns_empty(self):
        assert _collect_atom_names({"logic": "AND", "children": []}) == set()


class TestAtomMapping:
    def test_replaces_matching_names(self):
        tree = {
            "logic": "AND",
            "children": [
                {"atom": "price_move_up", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},
            ],
        }
        _apply_atom_mapping(tree, {"price_move_up": "price_move"})
        assert tree["children"][0]["atom"] == "price_move"

    def test_non_matching_unchanged(self):
        tree = {
            "logic": "AND",
            "children": [{"atom": "price_move", "params": {}}],
        }
        _apply_atom_mapping(tree, {"nonexistent": "price_move"})
        assert tree["children"][0]["atom"] == "price_move"


class TestFindAtomCorrections:
    def test_known_atom_no_correction(self):
        tree = {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}}
        assert _find_atom_corrections(tree) == {}

    def test_unknown_atom_returns_candidates(self):
        tree = {"atom": "price_jump", "params": {"ticker": "000001.SZ"}}
        corrections = _find_atom_corrections(tree)
        assert "price_jump" in corrections
        assert len(corrections["price_jump"]) > 0

    def test_time_atoms_skipped(self):
        """time_after 不在 EVALUATORS 中，但应被跳过而非标记为未知"""
        tree = {"atom": "time_after", "params": {"days": 3}}
        assert _find_atom_corrections(tree) == {}


# ═══════════════════════════════════════════════════
#  6. 动作解析  —— _parse_action（通过 compile 间接测试 + 直接测试）
# ═══════════════════════════════════════════════════


class TestActionParsing:
    @pytest.mark.asyncio
    async def test_buy_action(self):
        compiler = _make_compiler([
            # A 编译
            {"logic": "AND", "children": [{"atom": "price_move", "params": {"ticker": "600519.SH", "direction": "up", "pct": 5}}]},
            # B 评审

            # 动作解析
            {"action_type": "buy", "action_params": {"ticker": "600519.SH", "operation_type": "buy", "quantity": 100, "price": 1800.0}},
        ])
        result = await compiler.compile(
            name="test_buy",
            condition_nl="茅台涨5%",
            action_nl="买入100股茅台",
        )
        assert result["action_type"] == "buy"
        assert result["action_params"]["ticker"] == "600519.SH"

    @pytest.mark.asyncio
    async def test_sell_action(self):
        compiler = _make_compiler([
            {"logic": "AND", "children": [{"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "down", "pct": 3}}]},

            {"action_type": "sell", "action_params": {"close_reason": "触发止损"}},
        ])
        result = await compiler.compile(
            name="test_sell",
            condition_nl="股价跌了3%",
            action_nl="止损平仓",
        )
        assert result["action_type"] == "sell"
        assert result["action_params"]["close_reason"] == "触发止损"

    @pytest.mark.asyncio
    async def test_deep_analysis_action(self):
        compiler = _make_compiler([
            {"logic": "AND", "children": [{"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}}]},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(
            name="test_review",
            condition_nl="股价涨5%",
            action_nl="重新分析一下",
        )
        assert result["action_type"] == "deep_analysis"

    @pytest.mark.asyncio
    async def test_empty_action_defaults_to_deep_analysis(self):
        compiler = _make_compiler([
            {"logic": "AND", "children": [{"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}}]},

            # 空动作不触发 LLM 调用——直接返回 deep_analysis
        ])
        result = await compiler.compile(
            name="test_empty_action",
            condition_nl="股价涨5%",
            action_nl="",
        )
        assert result["action_type"] == "deep_analysis"
        assert result["action_params"] == {}

    @pytest.mark.asyncio
    async def test_invalid_action_type_falls_back(self):
        compiler = _make_compiler([
            {"logic": "AND", "children": [{"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}}]},

            {"action_type": "dance", "action_params": {}},
        ])
        result = await compiler.compile(
            name="test_invalid_action",
            condition_nl="股价涨5%",
            action_nl="跳个舞",
        )
        assert result["action_type"] == "deep_analysis"

    @pytest.mark.asyncio
    async def test_trade_converted_to_sell(self):
        compiler = _make_compiler([
            {"logic": "AND", "children": [{"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}}]},

            {"action_type": "trade", "action_params": {"close_reason": "条件平仓"}},
        ])
        result = await compiler.compile(
            name="test_trade",
            condition_nl="股价涨5%",
            action_nl="平仓",
        )
        assert result["action_type"] == "sell"


# ═══════════════════════════════════════════════════
#  7. 条件编译  —— 端到端（mock LLM）
# ═══════════════════════════════════════════════════


class TestConditionCompilation:
    @pytest.mark.asyncio
    async def test_simple_price_move(self):
        """单条件编译 → 单 child AND 被 promote 为裸原子"""
        compiler = _make_compiler([
            {"atom": "price_move", "params": {"ticker": "600519.SH", "direction": "up", "pct": 5}},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="茅台涨5%", condition_nl="茅台涨了5%", action_nl="")
        assert result["name"] == "茅台涨5%"
        assert result["condition"]["atom"] == "price_move"
        assert result["not_before"] is None
        assert result["not_after"] is None

    @pytest.mark.asyncio
    async def test_complex_nested_and_or(self):
        """茅台涨5% 且 (金叉 或 死叉)"""
        compiler = _make_compiler([
            {
                "logic": "AND",
                "children": [
                    {"atom": "price_move", "params": {"ticker": "600519.SH", "direction": "up", "pct": 5}},
                    {
                        "logic": "OR",
                        "children": [
                            {"atom": "ma_cross", "params": {"ticker": "600519.SH", "fast_period": "MA5", "slow_period": "MA20", "direction": "golden"}},
                            {"atom": "ma_cross", "params": {"ticker": "600519.SH", "fast_period": "MA5", "slow_period": "MA20", "direction": "death"}},
                        ],
                    },
                ],
            },

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="complex", condition_nl="茅台涨5%且(金叉或死叉)", action_nl="")
        root = result["condition"]
        assert root["logic"] == "AND"
        assert root["children"][0]["atom"] == "price_move"
        assert root["children"][1]["logic"] == "OR"
        assert len(root["children"][1]["children"]) == 2

    @pytest.mark.asyncio
    async def test_with_time_atoms(self):
        """三天后茅台突破2000"""
        compiler = _make_compiler([
            {
                "logic": "AND",
                "children": [
                    {"atom": "time_after", "params": {"days": 3}},
                    {"atom": "price_vs_level", "params": {"ticker": "600519.SH", "level": 2000, "relation": "above"}},
                ],
            },

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(
            name="time_compile",
            condition_nl="三天后茅台突破2000元",
            action_nl="",
            now=_NOW,
        )
        assert result["not_before"] is not None
        # 时间原子已被剥离
        atoms = _collect_atom_names(result["condition"])
        assert "time_after" not in atoms

    @pytest.mark.asyncio
    async def test_time_window_range(self):
        """三到五天内茅台涨5%"""
        compiler = _make_compiler([
            {
                "logic": "AND",
                "children": [
                    {"atom": "time_window", "params": {"days_min": 3, "days_max": 5}},
                    {"atom": "price_move", "params": {"ticker": "600519.SH", "direction": "up", "pct": 5}},
                ],
            },

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(
            name="time_window",
            condition_nl="三到五天内茅台涨5%",
            action_nl="",
            now=_NOW,
        )
        assert result["not_before"] is not None
        assert result["not_after"] is not None

    @pytest.mark.asyncio
    async def test_sector_condition(self):
        compiler = _make_compiler([
            {"atom": "sector_move", "params": {"sector": "半导体", "direction": "up", "pct": 3}},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="sector_test", condition_nl="半导体板块涨3%", action_nl="")
        assert result["condition"]["atom"] == "sector_move"
        assert result["condition"]["params"]["sector"] == "半导体"

    @pytest.mark.asyncio
    async def test_market_condition(self):
        compiler = _make_compiler([
            {"atom": "market_breadth", "params": {"up_down_ratio_min": 2.0}},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="market_test", condition_nl="全市场涨跌比大于2", action_nl="")
        assert result["condition"]["atom"] == "market_breadth"

    @pytest.mark.asyncio
    async def test_market_volume_condition(self):
        compiler = _make_compiler([
            {"atom": "market_volume", "params": {"amount_yi": 10000, "relation": "above"}},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="market_vol", condition_nl="两市成交额突破万亿", action_nl="")
        assert result["condition"]["atom"] == "market_volume"

    @pytest.mark.asyncio
    async def test_intraday_pattern(self):
        compiler = _make_compiler([
            {"atom": "intraday_reversal", "params": {"ticker": "600519.SH", "pattern": "shot_up_fall", "move_pct": 5}},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="intraday_test", condition_nl="茅台冲高回落5%", action_nl="")
        assert result["condition"]["atom"] == "intraday_reversal"

    @pytest.mark.asyncio
    async def test_volume_and_ma_combo(self):
        """放量且金叉"""
        compiler = _make_compiler([
            {
                "logic": "AND",
                "children": [
                    {"atom": "volume_ratio", "params": {"ticker": "000001.SZ", "multiplier": 1.5, "relation": "above"}},
                    {"atom": "ma_cross", "params": {"ticker": "000001.SZ", "fast_period": "MA5", "slow_period": "MA20", "direction": "golden"}},
                ],
            },

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="vol_ma", condition_nl="放量且金叉", action_nl="")
        atoms = _collect_atom_names(result["condition"])
        assert "volume_ratio" in atoms
        assert "ma_cross" in atoms

    @pytest.mark.asyncio
    async def test_consecutive_move(self):
        compiler = _make_compiler([
            {"atom": "consecutive_move", "params": {"ticker": "600519.SH", "direction": "up", "n_days": 3}},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="consecutive", condition_nl="茅台连涨三天", action_nl="")
        assert result["condition"]["atom"] == "consecutive_move"

    @pytest.mark.asyncio
    async def test_new_extreme(self):
        compiler = _make_compiler([
            {"atom": "new_extreme", "params": {"ticker": "600519.SH", "direction": "high", "n_days": 60}},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="extreme", condition_nl="茅台创60日新高", action_nl="")
        assert result["condition"]["atom"] == "new_extreme"

    @pytest.mark.asyncio
    async def test_gap_condition(self):
        compiler = _make_compiler([
            {"atom": "gap", "params": {"ticker": "600519.SH", "direction": "up", "min_pct": 2}},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="gap", condition_nl="茅台跳空高开2%", action_nl="")
        assert result["condition"]["atom"] == "gap"

    @pytest.mark.asyncio
    async def test_ma_alignment(self):
        compiler = _make_compiler([
            {"atom": "ma_alignment", "params": {"ticker": "600519.SH", "pattern": "bullish"}},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="ma_align", condition_nl="茅台均线多头排列", action_nl="")
        assert result["condition"]["atom"] == "ma_alignment"

    @pytest.mark.asyncio
    async def test_amplitude_wide(self):
        compiler = _make_compiler([
            {"atom": "amplitude_wide", "params": {"ticker": "600519.SH", "pct": 8, "relation": "above"}},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="amplitude", condition_nl="茅台振幅超过8%", action_nl="")
        assert result["condition"]["atom"] == "amplitude_wide"

    @pytest.mark.asyncio
    async def test_index_ticker(self):
        """使用指数代码的条件"""
        compiler = _make_compiler([
            {"atom": "price_move", "params": {"ticker": "000001.SH", "direction": "up", "pct": 1}},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="index", condition_nl="上证指数涨1%", action_nl="")
        assert result["condition"]["params"]["ticker"] == "000001.SH"


# ═══════════════════════════════════════════════════
#  8. B 评审  ——  通过 & 驳回
# ═══════════════════════════════════════════════════



# ═══════════════════════════════════════════════════
#  9. 名称修正  ——  板块名 & 原子名
# ═══════════════════════════════════════════════════


class TestNameCorrection:
    @pytest.mark.asyncio
    async def test_atom_correction_applied(self):
        """多原子树：已知 atom + 未知 atom → 统一校验触发修正"""
        compiler = _make_compiler([
            # A 编译：price_move 有效, price_fall 未知
            {"logic": "AND", "children": [
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "down", "pct": 3}},
                {"atom": "price_fall", "params": {"ticker": "000001.SZ", "direction": "down", "pct": 3}},
            ]},
            # 统一校验发现 price_fall 不在 EVALUATORS → LLM 返回修正后的完整条件树
            {"logic": "AND", "children": [
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "down", "pct": 3}},
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "down", "pct": 3}},
            ]},
            # B 评审

            # 动作解析
            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="atom_correction", condition_nl="股价跌了3%且价格下跌", action_nl="")
        atoms = _collect_atom_names(result["condition"])
        assert "price_move" in atoms
        assert "price_fall" not in atoms

    @pytest.mark.asyncio
    async def test_atom_correction_no_match(self):
        """多原子树中有一个无近似匹配的未知原子 → 拒绝编译，避免无效 trigger 入库"""
        compiler = _make_compiler([
            unknown_tree := {"logic": "AND", "children": [
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "down", "pct": 3}},
                {"atom": "xyzzy_nonexistent", "params": {}},
            ]},
            # 统一校验发现 xyzzy_nonexistent 无近似匹配，三次修正仍返回原树。
            unknown_tree,
            unknown_tree,
            unknown_tree,
        ])
        result = await compiler.compile(name="no_match", condition_nl="某个条件", action_nl="")
        assert "error" in result
        assert "xyzzy_nonexistent" in result["error"]


# ═══════════════════════════════════════════════════
#  10. 结构校验失败 → LLM 重试
# ═══════════════════════════════════════════════════


class TestStructureRetry:
    @pytest.mark.asyncio
    async def test_structure_validation_failure_retry(self):
        """首次编译 JSON 有 schema 错误 → 发回 LLM 修正 → 再校验"""
        compiler = _make_compiler([
            {"foo": "bar"},
            {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="retry", condition_nl="股价涨5%", action_nl="")
        assert result["condition"]["atom"] == "price_move"

    @pytest.mark.asyncio
    async def test_unknown_logic_fixed(self):
        """LLM 输出了非法 logic 值 → 发回修正"""
        compiler = _make_compiler([
            {"logic": "XOR", "children": [{"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}}]},
            {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="fix_logic", condition_nl="股价涨5%", action_nl="")
        assert result["condition"]["atom"] == "price_move"

    @pytest.mark.asyncio
    async def test_unknown_atom_in_structure_retry(self):
        """结构校验时发现未知原子 → 发回修正"""
        compiler = _make_compiler([
            {"atom": "stock_crash_999", "params": {}},
            # atom 不在 ATOM_DEFINITIONS → _validate_tree 捕获 → 发回修正
            {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "down", "pct": 10}},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="fix_atom", condition_nl="股价暴跌", action_nl="")
        assert result["condition"]["atom"] == "price_move"


# ═══════════════════════════════════════════════════
#  11. 边界情况 & 鲁棒性
# ═══════════════════════════════════════════════════


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_single_atom_no_logic_wrapper(self):
        """LLM 返回单原子叶（无 logic/children 包裹）"""
        compiler = _make_compiler([
            {"atom": "price_move", "params": {"ticker": "600519.SH", "direction": "up", "pct": 5}},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="single_atom", condition_nl="茅台涨5%", action_nl="")
        assert "atom" in result["condition"]
        assert result["condition"]["atom"] == "price_move"

    @pytest.mark.asyncio
    async def test_deeply_nested_tree(self):
        """深层嵌套 AND(OR(AND(OR(...))))"""
        deep_tree = {
            "logic": "AND",
            "children": [
                {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 1}},
                {
                    "logic": "OR",
                    "children": [
                        {"atom": "price_move", "params": {"ticker": "000002.SZ", "direction": "up", "pct": 2}},
                        {
                            "logic": "AND",
                            "children": [
                                {"atom": "volume_ratio", "params": {"ticker": "000002.SZ", "multiplier": 1.5, "relation": "above"}},
                                {"atom": "ma_cross", "params": {"ticker": "000002.SZ", "fast_period": "MA5", "slow_period": "MA20", "direction": "golden"}},
                            ],
                        },
                    ],
                },
            ],
        }
        compiler = _make_compiler([
            deep_tree,

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="deep", condition_nl="复杂嵌套条件", action_nl="")

        # 验证路径标注在深层树上也正确（_path 只打在原子叶上）
        annotated = result["condition"]
        assert annotated["children"][0]["_path"] == "0"
        or_node = annotated["children"][1]  # OR 逻辑组，无 _path
        assert or_node["children"][0]["_path"] == "1.0"
        inner_and = or_node["children"][1]  # AND 逻辑组，无 _path
        assert inner_and["children"][0]["_path"] == "1.1.0"
        assert inner_and["children"][1]["_path"] == "1.1.1"

    @pytest.mark.asyncio
    async def test_empty_condition_handled(self):
        """空条件描述仍能走完 pipeline"""
        compiler = _make_compiler([
            {"logic": "AND", "children": [{"atom": "market_breadth", "params": {"up_down_ratio_min": 1.0}}]},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="empty_nl", condition_nl=" ", action_nl="")
        assert result["name"] == "empty_nl"
        assert "condition" in result

    @pytest.mark.asyncio
    async def test_chinese_special_chars_in_name(self):
        """中文特殊字符不破坏 pipeline"""
        compiler = _make_compiler([
            {"logic": "AND", "children": [{"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 5}}]},

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(
            name="测试·触发（特殊符号）——验证",
            condition_nl="股价涨5%",
            action_nl="",
        )
        assert result["name"].startswith("测试")

    @pytest.mark.asyncio
    async def test_return_has_all_required_fields(self):
        compiler = _make_compiler([
            {
                "logic": "AND",
                "children": [
                    {"atom": "time_after", "params": {"days": 3}},
                    {"atom": "price_move", "params": {"ticker": "600519.SH", "direction": "up", "pct": 5}},
                ],
            },

            {"action_type": "buy", "action_params": {"ticker": "600519.SH", "operation_type": "buy", "quantity": 100}},
        ])
        result = await compiler.compile(
            name="full",
            condition_nl="三天后茅台涨5%",
            action_nl="买入100股茅台",
            now=_NOW,
        )
        assert "name" in result
        assert "condition" in result
        assert "action_type" in result
        assert "action_params" in result
        assert "not_before" in result
        assert "not_after" in result

    @pytest.mark.asyncio
    async def test_condition_with_only_or(self):
        """纯 OR 条件：涨停 或 跌停"""
        compiler = _make_compiler([
            {
                "logic": "OR",
                "children": [
                    {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "up", "pct": 9.9}},
                    {"atom": "price_move", "params": {"ticker": "000001.SZ", "direction": "down", "pct": 9.9}},
                ],
            },

            {"action_type": "deep_analysis", "action_params": {}},
        ])
        result = await compiler.compile(name="or_only", condition_nl="涨停或跌停", action_nl="")
        assert result["condition"]["logic"] == "OR"
        assert result["not_before"] is None
        assert result["not_after"] is None

    @pytest.mark.asyncio
    async def test_all_atom_categories_compilable(self):
        """验证 5 大类原子都能被正确编译"""
        test_cases = [
            # (条件描述, 预期原子名)
            ("股价涨了5%", "price_move"),
            ("茅台突破2000元", "price_vs_level"),
            ("茅台创60日新高", "new_extreme"),
            ("茅台跳空高开2%", "gap"),
            ("茅台连涨3天", "consecutive_move"),
            ("放量1.5倍", "volume_ratio"),
            ("换手率超过5%", "turnover_active"),
            ("振幅超过8%", "amplitude_wide"),
            ("MA5拐头向上", "ma_slope"),
            ("均线金叉", "ma_cross"),
            ("均线多头排列", "ma_alignment"),
            ("冲高回落5%", "intraday_reversal"),
            ("日内A字走势", "intraday_round_trip"),
            ("日内单边上涨", "intraday_trend"),
            ("半导体板块涨3%", "sector_move"),
            ("板块涨跌比大于0.6", "sector_breadth"),
            ("板块涨停家数大于5", "sector_limit_ratio"),
            ("全市场涨跌比大于2", "market_breadth"),
            ("成交额突破万亿", "market_volume"),
        ]
        for condition_nl, expected_atom in test_cases:
            compiler = _make_compiler([
                {"logic": "AND", "children": [{"atom": expected_atom, "params": _minimal_params(expected_atom)}]},
    
                {"action_type": "deep_analysis", "action_params": {}},
            ])
            result = await compiler.compile(
                name=f"category_{expected_atom}",
                condition_nl=condition_nl,
                action_nl="",
            )
            atoms = _collect_atom_names(result["condition"])
            assert expected_atom in atoms, f"Failed for: {condition_nl}"


# ── 参数测试辅助 ──────────────────────────────────


def _minimal_params(atom_name: str) -> dict:
    """为给定原子名生成最小合法参数集，避免 normalize_and_validate_tree 报错。"""
    defaults = {
        "price_move": {"ticker": "000001.SZ", "direction": "up", "pct": 5},
        "price_vs_level": {"ticker": "000001.SZ", "level": 2000, "relation": "above"},
        "new_extreme": {"ticker": "000001.SZ", "direction": "high", "n_days": 60},
        "gap": {"ticker": "000001.SZ", "direction": "up", "min_pct": 2},
        "consecutive_move": {"ticker": "000001.SZ", "direction": "up", "n_days": 3},
        "volume_ratio": {"ticker": "000001.SZ", "multiplier": 1.5, "relation": "above"},
        "turnover_active": {"ticker": "000001.SZ", "pct": 5, "relation": "above"},
        "amplitude_wide": {"ticker": "000001.SZ", "pct": 8, "relation": "above"},
        "ma_slope": {"ticker": "000001.SZ", "period": "MA5", "direction": "up"},
        "ma_cross": {"ticker": "000001.SZ", "fast_period": "MA5", "slow_period": "MA20", "direction": "golden"},
        "ma_alignment": {"ticker": "000001.SZ", "pattern": "bullish"},
        "intraday_reversal": {"ticker": "000001.SZ", "pattern": "shot_up_fall", "move_pct": 5},
        "intraday_round_trip": {"ticker": "000001.SZ", "direction": "A", "min_move_pct": 3},
        "intraday_trend": {"ticker": "000001.SZ", "direction": "up", "minutes": 30, "min_pct": 2},
        "sector_move": {"sector": "半导体", "direction": "up", "pct": 3},
        "sector_breadth": {"sector": "半导体", "up_ratio_min": 0.6},
        "sector_limit_ratio": {"sector": "半导体", "direction": "up", "min_count": 5},
        "market_breadth": {"up_down_ratio_min": 2.0},
        "market_volume": {"amount_yi": 10000, "relation": "above"},
    }
    return defaults.get(atom_name, {})


# ── 运行入口 ──────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
