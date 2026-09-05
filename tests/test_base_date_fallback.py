"""测试 price_move 的 base_date 容错功能"""

from datetime import datetime

from src.core.timezone import BEIJING_TZ
from src.triggers.eval_context import EvalContext
from src.triggers.evaluators import evaluate_atom


def test_base_date_fallback():
    """测试日期容错：指定非交易日时，使用最近的交易日"""
    ticker = "000001.SZ"

    # 构造历史数据：只有工作日（周一到周五）
    history_data = [
        {"date": "2024-03-01", "close": 100, "high": 101, "low": 99, "open": 99.5, "volume": 1000000},  # 周五
        # 2024-03-02, 03 是周末，无交易
        {"date": "2024-03-04", "close": 105, "high": 106, "low": 104, "open": 100, "volume": 1100000},  # 周一
        {"date": "2024-03-05", "close": 110, "high": 111, "low": 109, "open": 105, "volume": 1200000},  # 周二
        {"date": "2024-03-06", "close": 115, "high": 116, "low": 114, "open": 110, "volume": 1300000},  # 周三
        {"date": "2024-03-07", "close": 120, "high": 121, "low": 119, "open": 115, "volume": 1400000},  # 周四
        {"date": "2024-03-08", "close": 125, "high": 126, "low": 124, "open": 120, "volume": 1500000},  # 周五
    ]

    ctx = EvalContext(
        now=datetime.now(BEIJING_TZ),
        ticker_data={
            ticker: {
                "price": {"price": 125, "pct_chg": 4.17},
                "history": {"data": history_data},
            }
        },
        sector_data={},
        market_summary={},
    )

    # 测试 1: 指定周六 2024-03-02，应该回退到周五 2024-03-01
    result = evaluate_atom(
        "price_move",
        {
            "ticker": ticker,
            "direction": "up",
            "pct": 20,
            "base_date": "2024-03-02",  # 周六，非交易日
        },
        ctx,
    )

    print("测试 1: 指定周六（非交易日），应回退到最近交易日")
    print(f"  触发: {result['triggered']}")
    print(f"  详情: {result['detail']}")
    assert result["triggered"] is True
    assert result["detail"]["base_actual_date"] == "2024-03-01"  # 回退到周五
    assert result["detail"]["base_price"] == 100.0
    assert result["detail"]["actual_pct"] == 25.0  # (125-100)/100
    print("  ✓ 通过 - 正确回退到 2024-03-01\n")

    # 测试 2: 指定周日 2024-03-03，应该回退到周五 2024-03-01
    result = evaluate_atom(
        "price_move",
        {
            "ticker": ticker,
            "direction": "up",
            "pct": 20,
            "base_date": "2024-03-03",  # 周日，非交易日
        },
        ctx,
    )

    print("测试 2: 指定周日（非交易日），应回退到最近交易日")
    print(f"  触发: {result['triggered']}")
    print(f"  详情: {result['detail']}")
    assert result["triggered"] is True
    assert result["detail"]["base_actual_date"] == "2024-03-01"  # 回退到周五
    print("  ✓ 通过 - 正确回退到 2024-03-01\n")

    # 测试 3: 指定交易日中间的某天，应该使用该日期
    result = evaluate_atom(
        "price_move",
        {
            "ticker": ticker,
            "direction": "up",
            "pct": 10,
            "base_date": "2024-03-05",  # 周二，有交易
        },
        ctx,
    )

    print("测试 3: 指定正常交易日")
    print(f"  触发: {result['triggered']}")
    print(f"  详情: {result['detail']}")
    assert result["triggered"] is True
    assert result["detail"]["base_actual_date"] == "2024-03-05"  # 精确匹配
    assert result["detail"]["base_price"] == 110.0
    assert result["detail"]["actual_pct"] == 13.64  # (125-110)/110
    print("  ✓ 通过 - 精确使用 2024-03-05\n")

    # 测试 4: 指定一个在所有数据之前的日期
    result = evaluate_atom(
        "price_move",
        {
            "ticker": ticker,
            "direction": "up",
            "pct": 10,
            "base_date": "2024-02-20",  # 早于所有数据
        },
        ctx,
    )

    print("测试 4: 指定日期早于所有历史数据")
    print(f"  触发: {result['triggered']}")
    print(f"  原因: {result['detail'].get('reason')}")
    assert result["triggered"] is False
    assert "未找到" in result["detail"]["reason"]
    print("  ✓ 通过 - 正确返回错误\n")

    # 测试 5: 指定一个在当前日期之后的日期
    result = evaluate_atom(
        "price_move",
        {
            "ticker": ticker,
            "direction": "up",
            "pct": 10,
            "base_date": "2024-03-10",  # 晚于所有数据
        },
        ctx,
    )

    print("测试 5: 指定日期晚于所有历史数据，应使用最后一个交易日")
    print(f"  触发: {result['triggered']}")
    print(f"  详情: {result['detail']}")
    assert result["triggered"] is False  # 当前价格 = 基准价格，涨幅为0
    assert result["detail"]["base_actual_date"] == "2024-03-08"  # 最后一个交易日
    assert result["detail"]["base_price"] == 125.0
    assert result["detail"]["actual_pct"] == 0.0
    print("  ✓ 通过 - 使用最后一个交易日 2024-03-08\n")

    print("=" * 60)
    print("所有容错测试通过！✅")
    print("=" * 60)


def test_date_format_variations():
    """测试不同的日期格式"""
    ticker = "000001.SZ"

    history_data = [
        {"date": "2024-03-01", "close": 100, "high": 101, "low": 99, "open": 99.5, "volume": 1000000},
        {"date": "2024-03-05", "close": 120, "high": 121, "low": 119, "open": 115, "volume": 1400000},
    ]

    ctx = EvalContext(
        now=datetime.now(BEIJING_TZ),
        ticker_data={
            ticker: {
                "price": {"price": 120, "pct_chg": 0},
                "history": {"data": history_data},
            }
        },
        sector_data={},
        market_summary={},
    )

    # 测试各种日期格式
    date_formats = [
        "2024-03-03",  # 带连字符
        "20240303",    # 不带连字符
    ]

    for date_str in date_formats:
        result = evaluate_atom(
            "price_move",
            {
                "ticker": ticker,
                "direction": "up",
                "pct": 15,
                "base_date": date_str,
            },
            ctx,
        )

        print(f"测试日期格式: {date_str}")
        print(f"  触发: {result['triggered']}")
        print(f"  实际日期: {result['detail'].get('base_actual_date')}")
        assert result["detail"]["base_actual_date"] == "2024-03-01"  # 都应该回退到 03-01
        print("  ✓ 通过\n")


if __name__ == "__main__":
    test_base_date_fallback()
    print("\n")
    test_date_format_variations()
