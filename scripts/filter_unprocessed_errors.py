#!/usr/bin/env python3
"""过滤 error 日志：去掉已处理的条目，保留未处理的部分。"""
from pathlib import Path

INPUT = Path(r"c:\Users\33473\Desktop\quant\workflow\data\logs\2026-06\workflow-error-2026-06-23_01-19-52.log")
OUTPUT = Path(r"c:\Users\33473\Desktop\quant\workflow\data\logs\2026-06\workflow-error-2026-06-23_01-19-52_unprocessed.log")

# 已处理类型的匹配关键词（包含任一即丢弃）
HANDLED_PATTERNS = [
    # 1. orchestrator _run_significance retries exhausted → 已去掉栈打印
    "retries exhausted",
    # 2. csv_loader KB ingest 失败 → 已加重试
    "KB ingest",
    # 3. _retry_async 重试日志（信息性，非错误；网络抖动绝大部分已恢复）
    "orchestrator:_retry_async",
    # 4. _retry_api_call 重试日志（信息性；网络错误重试，现已加 ERROR 记录）
    "base:_retry_api_call",
    # 5. _invoke_one_tool 重试日志（信息性；工具调用重试，绝大部分恢复）
    '_invoke_one_tool:352',
    # 6. base:_invoke_one_tool 超时/异常重试（信息性）
    '_invoke_one_tool:344',
    '_invoke_one_tool:368',
]

def is_handled(line: str) -> bool:
    for pat in HANDLED_PATTERNS:
        if pat in line:
            return True
    return False

def main():
    kept = 0
    dropped = 0
    with open(INPUT, encoding="utf-8") as fin, open(OUTPUT, "w", encoding="utf-8") as fout:
        for line in fin:
            if is_handled(line):
                dropped += 1
            else:
                fout.write(line)
                kept += 1
    print(f"输入: {kept + dropped} 行")
    print(f"已处理(丢弃): {dropped} 行")
    print(f"未处理(保留): {kept} 行")
    print(f"输出: {OUTPUT}")

if __name__ == "__main__":
    main()
