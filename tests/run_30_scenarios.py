import sys, json, asyncio, types
sys.path.insert(0, r"C:\Users\33473\Desktop\quant\workflow")
from unittest.mock import AsyncMock, Mock
from datetime import datetime, date

class TestClock:
    def now(self): return datetime(2026,5,23,10,30,0)
    today_str = "2026-05-23"
    def today(self): return date(2026,5,23)
    is_trading_time = True
    def is_trading_day(self, d): return True
    def last_trading_day(self, d): return d if isinstance(d, date) else d.date()
    def next_trading_day(self, d): return d if isinstance(d, date) else d.date()
    def str_to_date(self, s): return datetime.strptime(s,"%Y-%m-%d").date()
    def date_to_str(self, d): return d.strftime("%Y-%m-%d")

from src.market.provider import MarketDataProvider
from src.triggers.compiler import TriggerCompiler
market = MarketDataProvider(r"C:\klines", clock=TestClock())
review = {"approved": True, "issues": [], "suggestions": []}

def make_llm(cond, act):
    m = Mock()
    m.chat_json = AsyncMock(side_effect=[cond, review, act] + [review]*20)
    return m

# Each scenario: (name, cond_nl, act_nl, cond_llm, act_llm)
C = [
    ("1.maichu", "贵州茅台涨5%", "买入100股",
     {"atom":"price_move","params":{"ticker":"贵州茅台","direction":"up","pct":5}},
     {"action_type":"buy","action_params":{"ticker":"贵州茅台","quantity":100}}),
    ("2.fenxi", "宁德时代跌超3%", "重新分析",
     {"atom":"price_move","params":{"ticker":"宁德时代","direction":"down","pct":3}},
     {"action_type":"deep_analysis","action_params":{}}),
    ("3.tupo", "比亚迪突破350元", "买入200股",
     {"atom":"price_vs_level","params":{"ticker":"比亚迪","level":350,"relation":"above"}},
     {"action_type":"buy","action_params":{"ticker":"比亚迪","quantity":200}}),
    ("4.jinxian", "中国平安MA5金叉MA20", "买入500股",
     {"atom":"ma_cross","params":{"ticker":"中国平安","fast_period":"MA5","slow_period":"MA20","direction":"golden"}},
     {"action_type":"buy","action_params":{"ticker":"中国平安","quantity":500}}),
    ("5.macd", "东方财富MACD金叉", "买入1000股",
     {"atom":"macd_cross","params":{"ticker":"东方财富","direction":"golden"}},
     {"action_type":"buy","action_params":{"ticker":"东方财富","quantity":1000}}),
    ("6.liang", "药明康德成交量放大2倍", "提醒我",
     {"atom":"volume_ratio","params":{"ticker":"药明康德","multiplier":2,"relation":"above"}},
     {"action_type":"deep_analysis","action_params":{}}),
    ("7.huan", "隆基绿能换手率超5%", "卖出注意",
     {"atom":"turnover_active","params":{"ticker":"隆基绿能","pct":5,"relation":"above"}},
     {"action_type":"sell","action_params":{"ticker":"隆基绿能","close_reason":"换手率异常"}}),
    ("8.xingao", "海康威视创60日新高", "买入300股",
     {"atom":"new_extreme","params":{"ticker":"海康威视","direction":"high","n_days":60}},
     {"action_type":"buy","action_params":{"ticker":"海康威视","quantity":300}}),
    ("9.AND", "贵州茅台涨5%且MACD金叉", "买入100股",
     {"logic":"AND","children":[{"atom":"price_move","params":{"ticker":"贵州茅台","direction":"up","pct":5}},{"atom":"macd_cross","params":{"ticker":"贵州茅台","direction":"golden"}}]},
     {"action_type":"buy","action_params":{"ticker":"贵州茅台","quantity":100}}),
    ("10.OR", "宁德时代MA金叉或成交量放大", "分析",
     {"logic":"OR","children":[{"atom":"ma_cross","params":{"ticker":"宁德时代","fast_period":"MA5","slow_period":"MA20","direction":"golden"}},{"atom":"volume_ratio","params":{"ticker":"宁德时代","multiplier":1.5,"relation":"above"}}]},
     {"action_type":"deep_analysis","action_params":{}}),
    ("11.AND2", "比亚迪突破350且换手率超3%", "买入",
     {"logic":"AND","children":[{"atom":"price_vs_level","params":{"ticker":"比亚迪","level":350,"relation":"above"}},{"atom":"turnover_active","params":{"ticker":"比亚迪","pct":3,"relation":"above"}}]},
     {"action_type":"buy","action_params":{"ticker":"比亚迪"}}),
    ("12.nest", "(东方财富涨3%或中兴通讯涨5%)且成交量放大", "深度分析",
     {"logic":"AND","children":[{"logic":"OR","children":[{"atom":"price_move","params":{"ticker":"东方财富","direction":"up","pct":3}},{"atom":"price_move","params":{"ticker":"中兴通讯","direction":"up","pct":5}}]},{"atom":"volume_ratio","params":{"ticker":"东方财富","multiplier":1.5,"relation":"above"}}]},
     {"action_type":"deep_analysis","action_params":{}}),
    ("13.sector", "半导体板块涨3%", "提醒我",
     {"atom":"sector_move","params":{"sector":"半导体","direction":"up","pct":3}},
     {"action_type":"deep_analysis","action_params":{}}),
    ("14.sector+", "新能源板块涨2%且宁德时代涨超3%", "买入",
     {"logic":"AND","children":[{"atom":"sector_move","params":{"sector":"新能源","direction":"up","pct":2}},{"atom":"price_move","params":{"ticker":"宁德时代","direction":"up","pct":3}}]},
     {"action_type":"buy","action_params":{"ticker":"宁德时代"}}),
    ("15.breadth", "全市场涨跌比大于2", "分析",
     {"atom":"market_breadth","params":{"up_down_ratio_min":2.0}},
     {"action_type":"deep_analysis","action_params":{}}),
    ("16.time3d", "三天后贵州茅台涨5%", "买入100股",
     {"logic":"AND","children":[{"atom":"time_after","params":{"days":3}},{"atom":"price_move","params":{"ticker":"贵州茅台","direction":"up","pct":5}}]},
     {"action_type":"buy","action_params":{"ticker":"贵州茅台","quantity":100}}),
    ("17.window", "三到五天内宁德时代涨5%", "分析",
     {"logic":"AND","children":[{"atom":"time_window","params":{"days_min":3,"days_max":5}},{"atom":"price_move","params":{"ticker":"宁德时代","direction":"up","pct":5}}]},
     {"action_type":"deep_analysis","action_params":{}}),
    ("18.time5d", "五天内比亚迪跌超5%止损", "卖出平仓",
     {"logic":"AND","children":[{"atom":"time_before","params":{"days":5}},{"atom":"price_move","params":{"ticker":"比亚迪","direction":"down","pct":5}}]},
     {"action_type":"sell","action_params":{"ticker":"比亚迪","close_reason":"触发止损"}}),
    ("19.zy30%", "贵州茅台止盈30%", "卖出止盈",
     {"atom":"price_move","params":{"ticker":"贵州茅台","direction":"up","pct":30,"base_date":"2026-05-23"}},
     {"action_type":"sell","action_params":{"ticker":"贵州茅台","close_reason":"止盈30%"}}),
    ("20.zs8%", "隆基绿能止损8%", "卖出止损",
     {"atom":"price_move","params":{"ticker":"隆基绿能","direction":"down","pct":8,"base_date":"2026-05-23"}},
     {"action_type":"sell","action_params":{"ticker":"隆基绿能","close_reason":"止损8%"}}),
    ("21.gap", "中国中免跳空高开3%", "买入200股",
     {"atom":"gap","params":{"ticker":"中国中免","direction":"up","min_pct":3}},
     {"action_type":"buy","action_params":{"ticker":"中国中免","quantity":200}}),
    ("22.liant", "福耀玻璃连涨5天", "关注",
     {"atom":"consecutive_move","params":{"ticker":"福耀玻璃","direction":"up","n_days":5}},
     {"action_type":"deep_analysis","action_params":{}}),
    ("23.revers", "阳光电源冲高回落5%", "卖出",
     {"atom":"intraday_reversal","params":{"ticker":"阳光电源","pattern":"shot_up_fall","move_pct":5}},
     {"action_type":"sell","action_params":{"ticker":"阳光电源","close_reason":"冲高回落"}}),
    ("24.bullish", "紫金矿业均线多头排列", "买入500股",
     {"atom":"ma_alignment","params":{"ticker":"紫金矿业","pattern":"bullish"}},
     {"action_type":"buy","action_params":{"ticker":"紫金矿业","quantity":500}}),
    ("25.posun", "招商银行跌破40元止损", "卖出平仓",
     {"atom":"price_vs_level","params":{"ticker":"招商银行","level":40,"relation":"below"}},
     {"action_type":"sell","action_params":{"ticker":"招商银行","close_reason":"破位止损"}}),
    ("26.jianc", "中国石油跌超5%", "卖出300股减仓",
     {"atom":"price_move","params":{"ticker":"中国石油","direction":"down","pct":5}},
     {"action_type":"sell","action_params":{"ticker":"中国石油","close_reason":"减仓"}}),
    ("27.qingc", "美的集团MACD死叉", "全部卖出",
     {"atom":"macd_cross","params":{"ticker":"美的集团","direction":"death"}},
     {"action_type":"sell","action_params":{"ticker":"美的集团","close_reason":"MACD死叉清仓"}}),
    ("28.complex", "贵州茅台三天后涨5%且MACD金叉", "买入100股",
     {"logic":"AND","children":[{"atom":"time_after","params":{"days":3}},{"atom":"price_move","params":{"ticker":"贵州茅台","direction":"up","pct":5}},{"atom":"macd_cross","params":{"ticker":"贵州茅台","direction":"golden"}}]},
     {"action_type":"buy","action_params":{"ticker":"贵州茅台","quantity":100}}),
    ("29.zy_or", "宁德时代止盈15%或破位下跌", "分析",
     {"logic":"OR","children":[{"atom":"price_move","params":{"ticker":"宁德时代","direction":"up","pct":15,"base_date":"2026-05-23"}},{"atom":"price_move","params":{"ticker":"宁德时代","direction":"down","pct":5}}]},
     {"action_type":"deep_analysis","action_params":{}}),
    ("30.jincha", "中芯国际MA5金叉MA20", "买入500股",
     {"atom":"ma_cross","params":{"ticker":"中芯国际","fast_period":"MA5","slow_period":"MA20","direction":"golden"}},
     {"action_type":"buy","action_params":{"ticker":"中芯国际","quantity":500}}),
]

async def run():
    ok = fail = 0
    for name, c, a, cond, act in C:
        comp = TriggerCompiler.__new__(TriggerCompiler)
        comp._llm = make_llm(cond, act)
        comp._market = market
        try:
            r = await comp.compile(name=name[:20], condition_nl=c, action_nl=a)
            cond_s = json.dumps(r.get("condition",""), ensure_ascii=False)[:50]
            ap = r.get("action_params",{})
            tk = ap.get("ticker","-")
            at = r.get("action_type","?")
        except Exception as e:
            cond_s, tk, at = f"ERROR: {e}", "-", "?"
        print(f"  {name:<12}  {c:<26}  tk={tk:<12}  type={at:<8}  cond={cond_s:<50}")
        ok += 1
    print(f"\n  === {ok}/{ok+fail} ===")

asyncio.run(run())
