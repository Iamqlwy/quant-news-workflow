from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langfuse import Langfuse
from langfuse.openai import AsyncOpenAI

from src.config import settings

LANGFUSE_PUBLIC_KEY = settings.langfuse_public_key
LANGFUSE_SECRET_KEY = settings.langfuse_secret_key
LANGFUSE_BASE_URL = settings.langfuse_base_url


def _get_llm_client() -> tuple[AsyncOpenAI, str]:
    if settings.llm_provider == "qwen":
        return (
            AsyncOpenAI(
                api_key=settings.qwen_api_key,
                base_url=settings.qwen_base_url,
            ),
            settings.qwen_model,
        )
    return (
        AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        ),
        settings.deepseek_model,
    )


async def _fake_tool(symbol: str) -> str:
    await asyncio.sleep(0.2)
    return f"{symbol} latest price snapshot: 12.34"


async def main() -> None:
    langfuse = Langfuse(
        public_key=LANGFUSE_PUBLIC_KEY,
        secret_key=LANGFUSE_SECRET_KEY,
        base_url=LANGFUSE_BASE_URL,
        environment="dev",
        httpx_client=httpx.Client(trust_env=False),
    )

    print("auth_check:", langfuse.auth_check())

    client, model = _get_llm_client()
    run_id = f"langfuse-validation-{uuid4().hex[:8]}"

    with langfuse.start_as_current_observation(
        name="validation-run",
        as_type="agent",
        input={"run_id": run_id, "provider": settings.llm_provider},
        metadata={"source": "scripts/validate_langfuse_integration.py"},
    ) as root:
        trace_id = langfuse.get_current_trace_id()
        print("trace_id:", trace_id)

        with langfuse.start_as_current_observation(
            name="market_snapshot_tool",
            as_type="tool",
            input={"symbol": "000001.SZ"},
            metadata={"validation": True},
        ) as tool_obs:
            tool_result = await _fake_tool("000001.SZ")
            tool_obs.update(output=tool_result)

        response = await client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=64,
            messages=[
                {
                    "role": "system",
                    "content": "You are a concise assistant.",
                },
                {
                    "role": "user",
                    "content": f"Reply with exactly: validation-ok-{run_id}",
                },
            ],
        )

        content = response.choices[0].message.content or ""
        root.update(
            output={
                "tool_result": tool_result,
                "llm_content": content,
            }
        )

    langfuse.flush()
    time.sleep(8)

    trace = langfuse.api.trace.get(trace_id)
    summary = {
        "trace_id": trace_id,
        "trace_name": getattr(trace, "name", None),
        "observations_count": len(trace.observations),
        "observation_types": [item.type for item in trace.observations],
        "observation_names": [item.name for item in trace.observations],
        "llm_content": content,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    langfuse.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
