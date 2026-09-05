"""pytest 日志配置 —— 测试中使用 loguru，输出到 stderr（pytest 自动捕获）"""

import sys
import pytest
from loguru import logger


@pytest.fixture(scope="session", autouse=True)
def configure_logging_for_tests():
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG",
        format="{time:HH:mm:ss.SSS} | {level: <8} | {name} | {message}",
        colorize=True,
    )
