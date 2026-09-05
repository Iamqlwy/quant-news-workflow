"""测试 price_move 的 base_date 功能"""

from datetime import datetime

from src.core.timezone import BEIJING_TZ
from src.triggers.eval_context import EvalContext
from src.triggers.evaluators import evaluate_atom


def test_price_move_with_base_date():
    """测试使用 base_date 参数（绝对日期）"""
    ticker = "000001.SZ"

    # 构造历史数据：从 2024-03-01 到 2024-03-10，价格从 100 涨到 135
    # 注意：字段名是 "date"（不是 "trade_date"），格式是 "YYYY-MM-DD"
    history_data = [
        {"date": "2024-03-01", "close": 100, "high": 101, "low": 99, "open": 99.5, "volume": 1000000},
        {"date": "2024-03-02", "close": 105, "high": 106, "low": 104, "open": 100, "volume": 1100000},
        {"date": "2024-03-03", "close": 110, "high": 111, "low": 109, "open": 105, "volume": 1200000},
        {"date": "2024-03-04", "close": 115, "high": 116, "low": 114, "open": 110, "volume": 1300000},
        {"date": "2024-03-05", "close": 120, "high": 121, "low": 119, "open": 115, "volume": 1400000},
        {"date": "2024-03-06", "close": 125, "high": 126, "low": 124, "open": 120, "volume": 1500000},
        {"date": "2024-03-07", "close": 130, "high": 131, "low": 129, "open": 125, "volume": 1600000},
        {"date": "2024-03-08", "close": 133, "high": 134, "low": 132, "open": 130, "volume": 1700000},
        {"date": "2024-03-09", "close": 134, "high": 135, "low": 133, "open": 133, "volume": 1800000},
        {"date": "2024-03-10", "close": 135, "high": 136, "low": 134, "open": 134, "volume": 1900000},
    ]

    ctx = EvalContext(
        now=datetime.now(BEIJING_TZ),
        ticker_data={
            ticker: {
                "price": {"price": 135, "pct_chg": 0.75},
                "history": {"data": history_data},
            }
        },
        sector_data={},
        market_summary={},
    )

    # 测试 1: 从 20240301 (价格100) 涨到现在 (价格135) = 35%
    result = evaluate_atom(
        "price_move",
        {
            "ticker": ticker,
            "direction": "up",
            "pct": 30,
            "base_date": "20240301",
        },
        ctx,
    )

    print("测试 1: 从 2024-03-01 涨超30%")
    print(f"  触发: {result['triggered']}")
    print(f"  详情: {result['detail']}")
    assert result["triggered"] is True
    assert result["detail"]["actual_pct"] == 35.0
    assert result["detail"]["base_date"] == "20240301"
    print("  ✓ 通过\n")

    # 测试 2: 从 20240305 (价格120) 涨到现在 (价格135) = 12.5%，不满足 15%
    result = evaluate_atom(
        "price_move",
        {
            "ticker": ticker,
            "direction": "up",
            "pct": 15,
            "base_date": "20240305",
        },
        ctx,
    )

    print("测试 2: 从 2024-03-05 涨超15%")
    print(f"  触发: {result['triggered']}")
    print(f"  详情: {result['detail']}")
    assert result["triggered"] is False
    assert result["detail"]["actual_pct"] == 12.5
    print("  ✓ 通过\n")

    # 测试 3: 支持带连字符的日期格式 2024-03-01
    result = evaluate_atom(
        "price_move",
        {
            "ticker": ticker,
            "direction": "up",
            "pct": 30,
            "base_date": "2024-03-01",
        },
        ctx,
    )

    print("测试 3: 带连字符的日期格式 2024-03-01")
    print(f"  触发: {result['triggered']}")
    print(f"  详情: {result['detail']}")
    assert result["triggered"] is True
    assert result["detail"]["actual_pct"] == 35.0
    print("  ✓ 通过\n")

    # 测试 4: 日期不存在
    result = evaluate_atom(
        "price_move",
        {
            "ticker": ticker,
            "direction": "up",
            "pct": 30,
            "base_date": "20240201",
        },
        ctx,
    )

    print("测试 4: 日期不存在")
    print(f"  触发: {result['triggered']}")
    print(f"  原因: {result['detail'].get('reason')}")
    assert result["triggered"] is False
    assert "未找到日期" in result["detail"]["reason"]
    print("  ✓ 通过\n")

    # 测试 5: 止盈场景 - 从买入日 (20240303, 价格110) 涨到现在 (135) = 22.7%，达到止盈20%
    result = evaluate_atom(
        "price_move",
        {
            "ticker": ticker,
            "direction": "up",
            "pct": 20,
            "base_date": "20240303",
        },
        ctx,
    )

    print("测试 5: 止盈场景 - 从买入日涨超20%")
    print(f"  触发: {result['triggered']}")
    print(f"  详情: {result['detail']}")
    assert result["triggered"] is True
    assert result["detail"]["actual_pct"] == 22.73
    assert result["detail"]["base_price"] == 110
    assert result["detail"]["current_price"] == 135
    print("  ✓ 通过\n")

    print("=" * 60)
    print("所有测试通过！✅")
    print("=" * 60)


def test_backward_compatibility():
    """测试向后兼容性 - lookback_days 仍然正常工作"""
    ticker = "000001.SZ"

    history_data = [
        {"date": "2024-03-01", "close": 100, "high": 101, "low": 99, "open": 99.5, "volume": 1000000},
        {"date": "2024-03-02", "close": 105, "high": 106, "low": 104, "open": 100, "volume": 1100000},
        {"date": "2024-03-03", "close": 110, "high": 111, "low": 109, "open": 105, "volume": 1200000},
        {"date": "2024-03-04", "close": 115, "high": 116, "low": 114, "open": 110, "volume": 1300000},
        {"date": "2024-03-05", "close": 120, "high": 121, "low": 119, "open": 115, "volume": 1400000},
    ]

    ctx = EvalContext(
        now=datetime.now(BEIJING_TZ),
        ticker_data={
            ticker: {
                "price": {"price": 120, "pct_chg": 4.35},
                "history": {"data": history_data},
            }
        },
        sector_data={},
        market_summary={},
    )

    # 测试 lookback_days=3: 从 3天前 (价格105) 涨到现在 (120) = 14.3%
    result = evaluate_atom(
        "price_move",
        {
            "ticker": ticker,
            "direction": "up",
            "pct": 10,
            "lookback_days": 3,
        },
        ctx,
    )

    print("向后兼容性测试: lookback_days=3")
    print(f"  触发: {result['triggered']}")
    print(f"  详情: {result['detail']}")
    assert result["triggered"] is True
    assert result["detail"]["lookback_days"] == 3
    print("  ✓ 通过\n")


if __name__ == "__main__":
    test_price_move_with_base_date()
    print("\n")
    test_backward_compatibility()
