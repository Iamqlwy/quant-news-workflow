"""诊断 _retry_async 中 ReadError 的真实异常类型和来源。
直接对 kbquant 发起请求，捕获并打印完整的异常链。
"""
import asyncio
import traceback
import httpx
from kbquant.client import QuantClient

KB_API_BASE = "http://localhost:8000/api/v1"
KB_API_KEY = "dev-api-key"  # 根据实际环境调整


async def diagnose_get(quant_client: QuantClient, raw_info_id: str):
    """直接调用 quant.information.get，打印完整异常链"""
    print(f"\n{'='*60}")
    print(f"Method: quant.information.get({raw_info_id})")
    print(f"{'='*60}")
    try:
        result = await quant_client.information.get(raw_info_id)
        print(f"SUCCESS: type={type(result).__name__}")
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        print(f"  exc.__cause__: {exc.__cause__!r}")
        print(f"  exc.__context__: {exc.__context__!r}")
        print("  Traceback:")
        traceback.print_exc()
        # 递归展开 cause chain
        print("\n  --- Exception chain ---")
        current = exc
        depth = 0
        while current is not None:
            print(f"  [{depth}] {type(current).__module__}.{type(current).__qualname__}: {str(current)[:200]}")
            current = current.__cause__ or current.__context__
            depth += 1
            if depth > 10:
                break


async def diagnose_get_many(quant_client: QuantClient, ids: list[str]):
    """直接调用 quant.information.get_many，打印完整异常链"""
    print(f"\n{'='*60}")
    print(f"Method: quant.information.get_many({len(ids)} ids)")
    print(f"{'='*60}")
    try:
        result = await quant_client.information.get_many(ids)
        print(f"SUCCESS: got {len(result)} items")
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        print(f"  exc.__cause__: {exc.__cause__!r}")
        print(f"  exc.__context__: {exc.__context__!r}")
        print("  Traceback:")
        traceback.print_exc()
        print("\n  --- Exception chain ---")
        current = exc
        depth = 0
        while current is not None:
            print(f"  [{depth}] {type(current).__module__}.{type(current).__qualname__}: {str(current)[:200]}")
            current = current.__cause__ or current.__context__
            depth += 1
            if depth > 10:
                break


def find_readerror_in_httpx():
    """搜索 httpx 中的 ReadError 定义"""
    import importlib, inspect

    # 检查 httpx 导出了什么
    candidates = []
    for name in dir(httpx):
        attr = getattr(httpx, name)
        if isinstance(attr, type) and issubclass(attr, Exception):
            candidates.append(name)
    print(f"\nhttpx 中的异常类: {sorted(candidates)}")

    # 尝试直接获取 ReadError
    if hasattr(httpx, "ReadError"):
        print(f"  httpx.ReadError EXISTS: {httpx.ReadError}")
    else:
        print("  httpx.ReadError NOT FOUND")

    # 也搜索子模块
    for mod_name in ["httpx._exceptions", "httpx._transports", "httpx._models"]:
        try:
            mod = importlib.import_module(mod_name)
            for name in dir(mod):
                if "read" in name.lower() or "error" in name.lower():
                    print(f"  {mod_name}.{name}")
        except ImportError:
            pass


async def main():
    quant_client = QuantClient(base_url=KB_API_BASE, api_key=KB_API_KEY)

    # 1. 先检查 httpx 中 ReadError 是否存在
    find_readerror_in_httpx()

    # 2. 测试 information.get (单个获取) — 用几个典型 ID
    print("\n\n=== 测试 information.get (单个) ===")
    test_ids = [
        "ba21d8f6-28e2-478b-a66e-782ff322c3ee",  # 日志中出现过的
        "00000000-0000-0000-0000-000000000001",  # 不存在的 ID
    ]
    for rid in test_ids:
        await diagnose_get(quant_client, rid)

    # 3. 测试 information.get_many (批量获取)
    print("\n\n=== 测试 information.get_many (批量) ===")
    await diagnose_get_many(quant_client, test_ids)


if __name__ == "__main__":
    asyncio.run(main())
