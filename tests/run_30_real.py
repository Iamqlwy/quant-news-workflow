import sys, json, asyncio, logging
from dataclasses import dataclass
from datetime import datetime, date
from typing import Any

sys.path.insert(0, r"C:\Users\33473\Desktop\quant\workflow")


class TestClock:
    def now(self): return datetime(2026, 5, 23, 10, 30, 0)
    today_str = "2026-05-23"
    def today(self): return date(2026, 5, 23)
    is_trading_time = True
    def is_trading_day(self, d): return True
    def last_trading_day(self, d): return d.date() if not isinstance(d, date) else d
    def next_trading_day(self, d): return d.date() if not isinstance(d, date) else d
    def str_to_date(self, s): return datetime.strptime(s, "%Y-%m-%d").date()
    def date_to_str(self, d): return d.strftime("%Y-%m-%d")


logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

CONCURRENCY = 100
TOTAL_SCENES = 100

@dataclass(frozen=True)
class Scene:
    seq: int
    name: str
    condition_nl: str
    action_nl: str
    focus_name: str
    action_detail: str


@dataclass
class SceneResult:
    seq: int
    name: str
    ok: bool
    db_ok: bool
    status: str
    action_type: str = "?"
    ticker: str = "-"
    fixed_ticker: bool = False
    db_id: str = ""
    error: str = ""
    condition_preview: str = ""


STOCKS = [
    "贵州茅台", "宁德时代", "比亚迪", "中国平安", "东方财富",
    "药明康德", "隆基绿能", "海康威视", "中兴通讯", "中芯国际",
    "招商银行", "美的集团", "紫金矿业", "中国中免", "福耀玻璃",
    "阳光电源", "五粮液", "平安银行", "万华化学", "立讯精密",
    "长江电力", "中国神华", "迈瑞医疗", "伊利股份", "恒瑞医药",
    "工业富联", "泸州老窖", "格力电器", "海尔智家", "中国建筑",
    "三一重工", "汇川技术", "兆易创新", "亿纬锂能", "天齐锂业",
    "赣锋锂业", "北方华创", "三花智控", "海天味业", "保利发展",
    "中国太保", "中国人寿", "中国电信", "中国移动", "中国联通",
    "上汽集团", "长城汽车", "长安汽车", "赛力斯", "科大讯飞",
    "用友网络", "金山办公", "爱尔眼科", "片仔癀", "山西汾酒",
    "洋河股份", "牧原股份",
]


def _buy_action(stock: str, quantity: int, detail: str) -> str:
    return (
        f"买入{stock}{quantity}股，理由：{detail}；"
        "执行前确认单票仓位不超过计划上限，并记录触发条件、时间和成交价格。"
    )


def _sell_action(stock: str, detail: str) -> str:
    return (
        f"卖出{stock}并平仓，原因：{detail}；"
        "如果当前仓位不足则卖出可用持仓，并记录退出依据和复盘标签。"
    )


def _analysis_action(stock: str, detail: str) -> str:
    return (
        f"对{stock}做深度分析并提醒我，重点复核：{detail}；"
        "输出趋势、量价、风险、仓位建议和下一步观察条件。"
    )


def _action(kind: str, stock: str, quantity: int, detail: str) -> str:
    if kind == "buy":
        return _buy_action(stock, quantity, detail)
    if kind == "sell":
        return _sell_action(stock, detail)
    return _analysis_action(stock, detail)


STOCK_TEMPLATES = [
    ("rise", lambda s, o, i: f"{s}涨超{3 + i % 5}%", "buy",
     lambda s, o, i: f"{s}价格涨幅达到入场阈值，优先确认趋势延续和成交活跃度"),
    ("fall", lambda s, o, i: f"{s}跌超{2 + i % 4}%", "sell",
     lambda s, o, i: f"{s}价格跌幅触发风险阈值，需要优先控制回撤"),
    ("break_up", lambda s, o, i: f"{s}突破{20 + (i % 8) * 10}元", "buy",
     lambda s, o, i: f"{s}向上突破关键价格位，观察突破有效性后执行"),
    ("break_down", lambda s, o, i: f"{s}跌破{20 + (i % 8) * 10}元止损", "sell",
     lambda s, o, i: f"{s}跌破关键价格位，按破位止损纪律处理"),
    ("ma_golden", lambda s, o, i: f"{s}MA5金叉MA20", "buy",
     lambda s, o, i: f"{s}短期均线向上穿越中期均线，趋势结构转强"),
    ("ma_death", lambda s, o, i: f"{s}MA5死叉MA20", "sell",
     lambda s, o, i: f"{s}短期均线向下穿越中期均线，趋势结构转弱"),
    ("macd_golden", lambda s, o, i: f"{s}MACD金叉", "buy",
     lambda s, o, i: f"{s}MACD出现金叉信号，复核量能后参与"),
    ("macd_death", lambda s, o, i: f"{s}MACD死叉", "sell",
     lambda s, o, i: f"{s}MACD转弱并出现死叉，先降低风险暴露"),
    ("vol_up", lambda s, o, i: f"{s}成交量放大{1.5 + (i % 4) * 0.2:.1f}倍", "analysis",
     lambda s, o, i: f"{s}成交量异常放大，判断是突破确认还是高位分歧"),
    ("vol_down", lambda s, o, i: f"{s}成交量低于20日均量0.7倍", "analysis",
     lambda s, o, i: f"{s}成交量明显萎缩，评估趋势持续性和流动性风险"),
    ("turnover_high", lambda s, o, i: f"{s}换手率超{4 + i % 5}%", "analysis",
     lambda s, o, i: f"{s}换手率进入活跃区间，核查是否存在短线过热"),
    ("turnover_low", lambda s, o, i: f"{s}换手率低于1%", "analysis",
     lambda s, o, i: f"{s}换手率偏低，判断是否缺乏资金关注"),
    ("new_high", lambda s, o, i: f"{s}创{30 + (i % 4) * 10}日新高", "buy",
     lambda s, o, i: f"{s}创阶段新高，跟踪趋势突破后的延续能力"),
    ("new_low", lambda s, o, i: f"{s}创{20 + (i % 5) * 10}日新低", "sell",
     lambda s, o, i: f"{s}创阶段新低，执行风险收缩并等待企稳信号"),
    ("gap_up", lambda s, o, i: f"{s}跳空高开{2 + i % 3}%", "analysis",
     lambda s, o, i: f"{s}跳空高开，确认缺口强度和盘中承接"),
    ("gap_down", lambda s, o, i: f"{s}跳空低开{2 + i % 3}%", "sell",
     lambda s, o, i: f"{s}跳空低开造成风险释放，优先保护本金"),
    ("up_days", lambda s, o, i: f"{s}连涨{3 + i % 3}天", "analysis",
     lambda s, o, i: f"{s}连续上涨后可能接近短线过热，复核追高风险"),
    ("down_days", lambda s, o, i: f"{s}连跌{3 + i % 3}天", "analysis",
     lambda s, o, i: f"{s}连续下跌后需要判断是趋势破坏还是超跌修复"),
    ("ma_bullish", lambda s, o, i: f"{s}均线多头排列", "buy",
     lambda s, o, i: f"{s}均线呈多头排列，趋势结构支持顺势参与"),
    ("ma_bearish", lambda s, o, i: f"{s}均线空头排列", "sell",
     lambda s, o, i: f"{s}均线呈空头排列，先退出等待趋势修复"),
    ("amplitude", lambda s, o, i: f"{s}日内振幅超过{6 + i % 5}%", "analysis",
     lambda s, o, i: f"{s}日内波动明显扩大，复核是否存在异常分歧"),
    ("shot_fall", lambda s, o, i: f"{s}冲高回落{3 + i % 4}%", "sell",
     lambda s, o, i: f"{s}冲高回落说明上方抛压较重，先锁定已有收益"),
    ("dip_recover", lambda s, o, i: f"{s}探底回升{3 + i % 4}%", "analysis",
     lambda s, o, i: f"{s}探底回升，判断是否形成有效承接"),
    ("intraday_up", lambda s, o, i: f"{s}最近5分钟上涨超过1%", "buy",
     lambda s, o, i: f"{s}盘中短线动能增强，确认成交跟随后再执行"),
    ("time_after", lambda s, o, i: f"三天后{s}涨超5%且MACD金叉", "buy",
     lambda s, o, i: f"{s}在三天后同时满足涨幅和MACD确认，属于延迟入场信号"),
    ("time_window", lambda s, o, i: f"三到五天内{s}跌超5%", "sell",
     lambda s, o, i: f"{s}在指定窗口内跌幅扩大，按时间窗口止损处理"),
    ("take_profit", lambda s, o, i: f"{s}止盈{12 + i % 9}%", "sell",
     lambda s, o, i: f"{s}达到计划止盈幅度，优先兑现收益"),
    ("stop_loss", lambda s, o, i: f"{s}止损{6 + i % 5}%", "sell",
     lambda s, o, i: f"{s}触及止损幅度，执行纪律性离场"),
    ("and_volume", lambda s, o, i: f"{s}涨超3%且成交量放大1.5倍", "buy",
     lambda s, o, i: f"{s}价格和成交量同步确认，信号质量高于单一涨幅"),
    ("nested", lambda s, o, i: f"({s}涨超3%或{o}MA5金叉MA20)且{s}成交量放大1.5倍", "analysis",
     lambda s, o, i: f"{s}与{o}之间出现组合触发，比较主线强弱后再决策"),
]


SECTOR_SCENES = [
    ("sector_semis", "半导体板块涨超3%", "北方华创", "半导体板块整体走强，检查核心设备股是否同步放量"),
    ("sector_ev", "新能源汽车板块涨超2%且宁德时代涨超3%", "宁德时代", "新能源汽车和龙头个股同步转强，复核持续性"),
    ("sector_ai", "人工智能板块涨超3%", "科大讯飞", "人工智能主题升温，检查龙头股量价结构"),
    ("sector_baijiu", "白酒板块跌超2%", "贵州茅台", "白酒板块走弱，评估贵州茅台防守能力"),
    ("sector_bank", "银行板块涨跌比大于1.8", "招商银行", "银行板块内部扩散增强，确认招商银行是否跟随"),
    ("sector_security", "证券板块涨停家数至少3家", "东方财富", "证券板块涨停扩散，复核东方财富弹性和成交"),
    ("sector_pv", "光伏设备板块涨超2%且隆基绿能涨超3%", "隆基绿能", "光伏设备板块和龙头共振，判断反弹级别"),
    ("sector_consumer", "消费电子板块涨超2%", "立讯精密", "消费电子板块转强，复核立讯精密趋势位置"),
    ("market_breadth", "全市场涨跌比大于2且平均涨幅超过1%", "东方财富", "市场宽度明显改善，检查券商和高弹性标的机会"),
    ("market_volume", "两市成交额突破10000亿元", "东方财富", "市场成交额突破万亿，评估交易活跃度对券商股的影响"),
]


def _build_stock_scenes(limit: int) -> list[Scene]:
    scenes: list[Scene] = []
    quantities = [100, 200, 300, 500, 800, 1000]
    i = 0
    while len(scenes) < limit:
        slug, cond_fn, kind, detail_fn = STOCK_TEMPLATES[i % len(STOCK_TEMPLATES)]
        stock = STOCKS[i % len(STOCKS)]
        other = STOCKS[(i + 11) % len(STOCKS)]
        detail = detail_fn(stock, other, i)
        scenes.append(
            Scene(
                seq=len(scenes) + 1,
                name=f"{len(scenes) + 1:03d}.{slug}",
                condition_nl=cond_fn(stock, other, i),
                action_nl=_action(kind, stock, quantities[i % len(quantities)], detail),
                focus_name=stock,
                action_detail=detail,
            )
        )
        i += 1
    return scenes


def _build_scenes() -> list[Scene]:
    scenes = _build_stock_scenes(TOTAL_SCENES - len(SECTOR_SCENES))
    for slug, condition_nl, focus_name, detail in SECTOR_SCENES:
        scenes.append(
            Scene(
                seq=len(scenes) + 1,
                name=f"{len(scenes) + 1:03d}.{slug}",
                condition_nl=condition_nl,
                action_nl=_analysis_action(focus_name, detail),
                focus_name=focus_name,
                action_detail=detail,
            )
        )
    return scenes


SCENES = _build_scenes()
assert len(SCENES) == TOTAL_SCENES


def _collect_condition_tickers(condition: Any) -> list[str]:
    tickers: list[str] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        params = node.get("params")
        if isinstance(params, dict) and params.get("ticker"):
            tickers.append(str(params["ticker"]))
        for child in node.get("children") or []:
            walk(child)

    walk(condition)
    return list(dict.fromkeys(tickers))


def _resolve_name_to_ticker(market: Any, name: str | None) -> str | None:
    if not name:
        return None
    if "." in name:
        return name
    code = market.resolve_index_name(name)
    if code:
        return code
    matches = market.resolve_stock_ticker(name)
    if matches:
        return matches[0][0]
    return None


def _enrich_action_params(scene: Scene, compiled: dict, market: Any) -> tuple[dict, bool]:
    params = dict(compiled.get("action_params") or {})
    action_type = compiled.get("action_type", "")
    condition_tickers = _collect_condition_tickers(compiled.get("condition"))

    existing = params.get("ticker")
    resolved_existing = _resolve_name_to_ticker(market, str(existing)) if existing else None
    if resolved_existing:
        params["ticker"] = resolved_existing
    elif existing and "." not in str(existing):
        params.pop("ticker", None)

    missing_before = not params.get("ticker")
    fallback_ticker = (
        condition_tickers[0] if condition_tickers else _resolve_name_to_ticker(market, scene.focus_name)
    )
    if not params.get("ticker") and fallback_ticker:
        params["ticker"] = fallback_ticker

    if condition_tickers:
        params.setdefault("condition_tickers", condition_tickers)
    if len(condition_tickers) > 1:
        params.setdefault("tickers", condition_tickers)

    params.setdefault("focus_name", scene.focus_name)
    params.setdefault("source_condition_nl", scene.condition_nl)
    params.setdefault("source_action_nl", scene.action_nl)
    params.setdefault("action_detail", scene.action_detail)

    if action_type == "buy":
        params.setdefault("operation_type", "buy")
        params.setdefault("rationale", scene.action_detail)
    elif action_type == "sell":
        params.setdefault("operation_type", "sell")
        params.setdefault("close_reason", scene.action_detail)
    elif action_type == "deep_analysis":
        params.setdefault("analysis_focus", scene.action_detail)

    return params, missing_before and bool(params.get("ticker"))


def _condition_preview(condition: Any) -> str:
    return json.dumps(condition, ensure_ascii=False, separators=(",", ":"))[:120]


async def _run_scene(
    scene: Scene,
    compiler: Any,
    market: Any,
    sem: asyncio.Semaphore,
    lock: asyncio.Lock,
    progress: dict,
) -> SceneResult:
    async with sem:
        try:
            compiled = await compiler.compile(
                name=scene.name,
                condition_nl=scene.condition_nl,
                action_nl=scene.action_nl,
            )
        except Exception as e:
            result = SceneResult(scene.seq, scene.name, False, False, "COMPILE_ERROR", error=str(e))
            await _print_progress(result, lock, progress)
            return result

        if "error" in compiled:
            result = SceneResult(
                scene.seq,
                scene.name,
                False,
                False,
                "COMPILE_FAILED",
                error=str(compiled["error"])[:200],
            )
            await _print_progress(result, lock, progress)
            return result

        action_params, fixed_ticker = _enrich_action_params(scene, compiled, market)
        compiled["action_params"] = action_params
        ticker = str(action_params.get("ticker") or "-")

        if not action_params.get("ticker"):
            result = SceneResult(
                scene.seq,
                scene.name,
                False,
                False,
                "PARAM_ERROR",
                action_type=compiled.get("action_type", "?"),
                ticker=ticker,
                condition_preview=_condition_preview(compiled.get("condition")),
                error="action_params missing ticker after enrichment",
            )
            await _print_progress(result, lock, progress)
            return result

        try:
            from src.tools._db import create_trigger_record

            tid = await create_trigger_record(
                name=scene.name,
                condition=compiled["condition"],
                action_type=compiled["action_type"],
                action_params=compiled.get("action_params"),
                trade_id=None,
                source_task_id=None,
                source_analysis_id=None,
                not_before=datetime.fromisoformat(compiled["not_before"]) if compiled.get("not_before") else None,
                not_after=datetime.fromisoformat(compiled["not_after"]) if compiled.get("not_after") else None,
            )
        except Exception as e:
            result = SceneResult(
                scene.seq,
                scene.name,
                False,
                False,
                "DB_ERROR",
                action_type=compiled.get("action_type", "?"),
                ticker=ticker,
                fixed_ticker=fixed_ticker,
                condition_preview=_condition_preview(compiled.get("condition")),
                error=str(e),
            )
            await _print_progress(result, lock, progress)
            return result

        result = SceneResult(
            scene.seq,
            scene.name,
            True,
            True,
            "OK",
            action_type=compiled.get("action_type", "?"),
            ticker=ticker,
            fixed_ticker=fixed_ticker,
            db_id=str(tid)[:8],
            condition_preview=_condition_preview(compiled.get("condition")),
        )
        await _print_progress(result, lock, progress)
        return result


async def _print_progress(result: SceneResult, lock: asyncio.Lock, progress: dict) -> None:
    async with lock:
        progress["done"] += 1
        fix = " fixed_ticker" if result.fixed_ticker else ""
        tail = f" DB={result.db_id}" if result.db_id else f" {result.error[:80]}"
        print(
            f"[{progress['done']:03d}/{TOTAL_SCENES}] {result.name:<22} "
            f"{result.status:<14} type={result.action_type:<13} ticker={result.ticker:<12}{fix}{tail}",
            flush=True,
        )


async def run():
    from src.market.provider import MarketDataProvider
    from src.triggers.compiler import TriggerCompiler

    print(f"=== 开始真实编译: {len(SCENES)} 条条件, 并发={CONCURRENCY} ===", flush=True)
    market = MarketDataProvider(r"C:\klines", clock=TestClock())
    compiler = TriggerCompiler(market=market)
    sem = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()
    progress = {"done": 0}
    tasks = [
        asyncio.create_task(_run_scene(scene, compiler, market, sem, lock, progress))
        for scene in SCENES
    ]
    results = await asyncio.gather(*tasks)
    results.sort(key=lambda r: r.seq)

    db_ok = sum(1 for r in results if r.db_ok)
    fail = len(results) - db_ok
    fixed = sum(1 for r in results if r.fixed_ticker)
    missing = sum(1 for r in results if r.status == "PARAM_ERROR")

    print(
        f"\n=== 完成: {db_ok}/{len(SCENES)} 编译+入库, "
        f"{fixed} 条补齐 ticker, {missing} 条仍缺 ticker, {fail} 失败 ==="
    )

    failures = [r for r in results if not r.ok]
    if failures:
        print("\n失败明细（前 20 条）:")
        for r in failures[:20]:
            print(f"  {r.name:<22} {r.status:<14} ticker={r.ticker:<12} err={r.error[:160]}")


if __name__ == "__main__":
    asyncio.run(run())
