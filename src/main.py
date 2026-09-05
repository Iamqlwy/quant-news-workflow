"""Workflow 主入口 —— 重构版本

架构说明：
- WorkflowComponents: 组件工厂，负责创建和管理所有核心组件
- SimulationRunner: 模拟模式运行器，处理 CSV 回放
- RealtimeRunner: 实时模式运行器，处理实时数据流
- TriggerCallbackHandler: 触发器回调处理器，分发触发动作
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

import asyncio
import contextlib

from loguru import logger
from rich.console import Console
from rich.text import Text

from src.config import settings
from src.observability import shutdown_langfuse
from src.runners.components import WorkflowComponents
from src.runners.realtime import RealtimeRunner
from src.runners.simulation import SimulationRunner
from src.runners.trigger_handler import TriggerCallbackHandler
from src.triggers.engine import TriggerEngine
from src.workflow_logging import setup_logging


class WorkflowApp:
    """工作流应用主类

    职责：
    - 初始化组件（委托给 WorkflowComponents）
    - 创建运行器（SimulationRunner 或 RealtimeRunner）
    - 管理应用生命周期
    """

    def __init__(self) -> None:
        # 根据配置选择模式
        self.simulation_mode = settings.simulation_enabled

        # 初始化组件
        self.components = WorkflowComponents(simulation_mode=self.simulation_mode)

        # 创建触发器回调处理器
        self.trigger_handler = TriggerCallbackHandler(
            quant=self.components.quant,
            get_now=lambda: self.components.clock.now,
        )

        # 初始化触发器引擎（需要回调函数）
        trigger_engine = TriggerEngine(
            self.components.market,
            self.trigger_handler.handle,
        )
        self.components.set_trigger_engine(trigger_engine)

        # 创建运行器
        if self.simulation_mode:
            self.runner = SimulationRunner(self.components)
            logger.info("模拟模式已初始化")
        else:
            self.runner = RealtimeRunner(self.components)
            logger.info("实时模式已初始化")

    async def start(self) -> None:
        """启动工作流"""
        mode = "simulation" if self.simulation_mode else "realtime"
        logger.info("启动工作流: mode={}", mode)

        with contextlib.suppress(asyncio.CancelledError):
            await self.runner.run()

    async def shutdown(self) -> None:
        """关闭工作流"""
        logger.info("正在关闭工作流...")
        await self.components.shutdown()
        shutdown_langfuse()
        logger.info("工作流已关闭")


def _print_logo() -> None:
    """打印启动 Logo"""
    console = Console()
    ascii_logo = """
 █████   ███   █████ ███████████ ██████████   █████
▒▒███   ▒███  ▒▒███ ▒▒███▒▒▒▒▒▒█▒▒███▒▒▒▒███ ▒▒███
 ▒███   ▒███   ▒███  ▒███   █ ▒  ▒███   ▒▒███ ▒███
 ▒███   ▒███   ▒███  ▒███████    ▒███    ▒███ ▒███
 ▒▒███  █████  ███   ▒███▒▒▒█    ▒███    ▒███ ▒███
  ▒▒▒█████▒█████▒    ▒███  ▒     ▒███    ███  ▒███      █
    ▒▒███ ▒▒███      █████       ██████████   ███████████
     ▒▒▒   ▒▒▒      ▒▒▒▒▒       ▒▒▒▒▒▒▒▒▒▒   ▒▒▒▒▒▒▒▒▒▒▒
"""

    for line in ascii_logo.splitlines():
        non_space_indices = [i for i, c in enumerate(line) if c.strip()]
        if not non_space_indices:
            console.print(line)
            continue

        total = len(non_space_indices)
        text_line = Text()
        for i, ch in enumerate(line):
            if i in non_space_indices:
                t = non_space_indices.index(i) / (total - 1) if total > 1 else 0
                r = int(t * 255)
                b = int((1 - t) * 255)
                text_line.append(ch, style=f"rgb({r},0,{b})")
            else:
                text_line.append(ch)
        console.print(text_line)


async def main() -> None:
    """主入口函数"""
    _print_logo()
    setup_logging()

    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=200))

    app: WorkflowApp | None = None
    try:
        app = WorkflowApp()
        await app.start()
    except asyncio.CancelledError:
        logger.info("收到中断信号")
    finally:
        if app is not None:
            await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
