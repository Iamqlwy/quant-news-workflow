import sys
from pathlib import Path

import pytest
from loguru import logger

sys.path.append(str(Path(__file__).parent.parent))

from src.workflow_logging import format_progress_fields, progress_span


@pytest.fixture
def captured_messages():
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]), format="{message}")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def test_progress_span_logs_start_and_complete(captured_messages):
    with progress_span("阶段测试", task_id="T-1", stage="demo"):
        pass

    assert any("阶段测试 开始" in message and "task_id=T-1" in message for message in captured_messages)
    assert any("阶段测试 完成" in message and "elapsed_s=" in message for message in captured_messages)


def test_format_progress_fields_skips_empty_values():
    result = format_progress_fields(task_id="T-2", empty="", skipped=None, elapsed_s=1.234)

    assert "task_id=T-2" in result
    assert "elapsed_s=1.2" in result
    assert "empty=" not in result
    assert "skipped=" not in result
