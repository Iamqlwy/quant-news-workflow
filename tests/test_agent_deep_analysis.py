"""深度分析 Agent 集成测试 —— 单条资讯全流程"""
import asyncio
from loguru import logger
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

from src.agents.deep_analysis import create_deep_analysis_agent
from src.config import settings
from src.tools import init_ctx


async def main():
    # ── 1. 设置 mock 依赖 ──
    kb = AsyncMock()
    market = Mock()
    clock = Mock()
    clock.today = Mock()
    clock.today.isoformat.return_value = "2025-07-28"

    # KB mock 返回值
    kb.search_kb.return_value = {
        "items": [
            {"title": "影石Insta360上市进展：已提交科创板IPO申请", "body": "影石Insta360于2025年Q1提交科创板IPO申请...", "source": "sina"},
            {"title": "消费级无人机市场2025年规模突破500亿", "body": "消费级无人机市场持续增长...", "source": "eastmoney"},
            {"title": "全景相机技术路线对比：影石vs GoPro", "body": "全景相机技术分析...", "source": "cls"},
        ]
    }
    kb.get_lessons.return_value = {"items": [
        {"title": "新产品发布→股价短期脉冲，持续性看销量数据"},
        {"title": "概念炒作退潮后需及时止盈"},
    ]}
    kb.get_similar_cases.return_value = {"items": [
        {"title": "大疆发布Mini 4 Pro后股价反应", "body": "发布后一周内上涨12%，随后回落..."},
        {"title": "GoPro发布Hero 12后市场反应", "body": "发布当日涨8%，因竞争加剧次月跌15%"},
    ]}
    kb.get_current_state.return_value = {
        "core_logic": "影石是全景相机龙头，IPO预期支撑估值",
        "primary_drivers": ["产品创新", "消费电子周期", "IPO进度"],
        "risks": ["竞争加剧", "消费电子需求下滑"],
        "focus_points": ["新产品线拓展", "海外市场增长"],
        "recent_changes": "无人机产品线即将发布"
    }
    kb.create_analysis.return_value = {"id": "analysis-test-001"}
    kb.create_trade.return_value = {"id": "trade-test-001"}
    kb.update_node_state.return_value = {"status": "ok"}
    kb._get.return_value = {"summary": "宏观环境中性偏积极", "content": "..."}

    # Market mock
    market.get_realtime_price = AsyncMock(return_value={"price": 88.5, "change_pct": 3.2, "volume": 25000000, "open": 86.0, "high": 89.2, "low": 85.8})
    market.get_sector_overview = AsyncMock(return_value={"sector": "消费电子", "avg_change_pct": 1.8, "up_count": 28, "down_count": 12, "volume_ratio": 1.3})
    market.get_technical_indicators = AsyncMock(return_value={"ma5": 85.0, "ma20": 82.0, "ma60": 78.0, "rsi": 62, "macd": 1.2, "bollinger_upper": 92, "bollinger_middle": 85, "bollinger_lower": 78, "volume_ratio": 1.5})

    # Prefs mock (via kb.preferences)
    kb.preferences = Mock()
    kb.preferences.get_industry_cognition = AsyncMock()
    kb.preferences.get_industry_cognition.return_value = Mock(
        text="消费电子行业偏好：关注新产品周期、重视销量数据验证、警惕炒作退潮", append_count=2
    )
    kb.preferences.append_industry_cognition = AsyncMock()
    kb.preferences.append_industry_cognition.return_value = Mock(
        sector="消费电子", status="appended"
    )

    # ── 2. 注入依赖 ──
    init_ctx(quant=kb, market=market, compiler=None, clock=clock)

    # ── 3. 创建 Agent ──
    agent = create_deep_analysis_agent()

    # ── 4. 输入资讯 ──
    title = "影石无人机品牌官宣，首款全景无人机将于8月开启公测"
    body = (
        '7月28日，消费级无人机品牌"影翎Antigravity"官宣亮相，计划推出首款"全景无人机"，'
        '8月开启公测招募。据介绍，影翎Antigravity由影石Insta360和第三方共同孵化。'
        '这款全景无人机无需外接全景相机配件，即可直接拍摄高质量全景画面，'
        '支持实时数据传输，并能够实时调整拍摄参数。'
    )
    context = {
        "title": title,
        "body": body,
        "source": "36氪",
        "published_at": "2025-07-28T10:30:00",
        "raw_info_id": "test-info-001",
        "task_id": "test-task-001",
    }

    log_path = Path("data/deep_analysis_log.txt")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w", encoding="utf-8")

    def log(msg: str):
        logger.info(msg)
        log_file.write(msg + "\n")
        log_file.flush()

    log(f"=== 深度分析 Agent 测试 ===")
    log(f"标题: {title}")
    log(f"时间: {datetime.now().strftime('%H:%M:%S')}")
    log(f"模型: {settings.deepseek_model}")
    log("")

    # ── 5. 运行 ──
    try:
        started = datetime.now()
        result = await agent.run(context)
        elapsed = (datetime.now() - started).total_seconds()
        content = result.get("content", "")

        log(f"\n{'='*60}")
        log(f"耗时: {elapsed:.1f}s")
        log(f"输出长度: {len(content)} 字符")
        log(f"\n=== Agent 最终输出 ===")
        log(content[:2000])
        if len(content) > 2000:
            log(f"\n... (共 {len(content)} 字符，已截断)")
        log(f"\n=== 测试结果: 通过 ===")
        log(f"详细日志: {log_path}")

    except Exception as e:
        elapsed = (datetime.now() - started).total_seconds() if 'started' in dir() else 0
        log(f"耗时: {elapsed:.1f}s")
        log(f"=== 测试结果: 失败 ===")
        log(f"异常类型: {type(e).__name__}")
        log(f"异常信息: {e}")
        import traceback
        log(f"\n完整 traceback:")
        log(traceback.format_exc())

    finally:
        log_file.close()


if __name__ == "__main__":
    asyncio.run(main())
