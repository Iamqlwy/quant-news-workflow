"""修复 ChatDeepSeek：_get_request_payload 回传 reasoning_content"""
import os, json
from loguru import logger
from dotenv import load_dotenv
from langchain_deepseek import ChatDeepSeek
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

load_dotenv()


class FixedChatDeepSeek(ChatDeepSeek):
    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        # 遍历 input 和 payload，将 additional_kwargs 中的 reasoning_content 写回请求
        for i, msg in enumerate(input_):
            if isinstance(msg, AIMessage):
                rc = msg.additional_kwargs.get("reasoning_content")
                if rc:
                    payload["messages"][i]["reasoning_content"] = rc
        return payload


llm = FixedChatDeepSeek(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    model="deepseek-v4-flash",
    temperature=0,
)


@tool
def get_stock_price(ticker: str) -> str:
    """获取股票实时价格"""
    return f"{ticker} 当前价格 88.5 元，涨幅 3.2%"


@tool
def get_market_breadth() -> str:
    """获取市场宽度数据"""
    return "全市场上涨 2500 家，下跌 1500 家，平均涨幅 0.5%"


bound = llm.bind_tools([get_stock_price, get_market_breadth])

# ---- 模拟 Agent ReAct 循环 ----
msgs = [HumanMessage(content="帮我分析一下 688001.SH 是否值得买入")]

for step in range(5):
    logger.info(f"\n=== Step {step+1} ===")
    r = bound.invoke(msgs)
    logger.info(f"content: {str(r.content)[:100]}")
    tc = r.tool_calls
    if not tc:
        logger.info(">> 无工具调用，Agent 结束")
        logger.info(f"\n最终输出:\n{r.content}")
        break
    logger.info(f"tool_calls: {[(t['name'], t['args']) for t in tc]}")
    rc_len = len(r.additional_kwargs.get('reasoning_content', '') or '')
    logger.info(f"reasoning_content: {rc_len} 字符")

    msgs.append(r)
    for t in tc:
        fn = get_stock_price if t["name"] == "get_stock_price" else get_market_breadth
        result = fn.invoke(t["args"])
        msgs.append(ToolMessage(content=result, tool_call_id=t["id"]))
        logger.info(f"  工具结果: {result[:60]}")
