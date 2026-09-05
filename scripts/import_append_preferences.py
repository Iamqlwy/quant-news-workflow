"""
从 append_preference_36h.json 读取数据，直接通过 HTTP POST 写入 kbquant。
去重：相同 sector 只保留最早的一条。
"""
import asyncio
import json
from urllib.parse import quote

import httpx

API_BASE = "http://localhost:8000/api/v1"
API_KEY = "workflow-agent-key"
DATA_FILE = "scripts/append_preference_36h.json"


def deduplicate(records: list[dict]) -> list[dict]:
    """按 sector 去重，保留最早（startTime 最小）的记录。"""
    seen: dict[str, dict] = {}
    for r in records:
        sector = r["sector"]
        if sector in seen:
            if r["startTime"] < seen[sector]["startTime"]:
                seen[sector] = r
        else:
            seen[sector] = r
    return sorted(seen.values(), key=lambda r: r["startTime"])


async def append_industry(session: httpx.AsyncClient, sector: str, text: str) -> dict:
    """双重编码 sector 以解决路径中 / 被 FastAPI 路由误解的问题。"""
    encoded = quote(quote(sector, safe=""), safe="")
    url = f"/preferences/{encoded}/cognition"
    resp = await session.post(url, json={"text": text}, headers={"X-API-Key": API_KEY})
    if resp.is_success:
        return resp.json()
    resp.raise_for_status()


async def main():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        records = json.load(f)

    deduped = deduplicate(records)
    print(f"原始 {len(records)} 条，去重后 {len(deduped)} 条\n")

    async with httpx.AsyncClient(base_url=API_BASE, timeout=30) as session:
        for r in deduped:
            sector = r["sector"]
            text = r["text"]
            start = r["startTime"]
            print(f"写入 sector={sector} (time={start[:19]})...")
            try:
                resp = await append_industry(session, sector, text)
                print(f"  -> status={resp['status']}, sector={resp['sector']}")
            except Exception as e:
                print(f"  -> 失败: {e}")

    print("\n完成")


if __name__ == "__main__":
    asyncio.run(main())
