"""工作流组件工厂 - 创建和管理所有核心组件"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from kbquant.client import QuantClient
from loguru import logger

from src.config import settings
from src.core.clock import Clock, TimeConfig
from src.core.scheduler import Scheduler
from src.core.timezone import BEIJING_TZ
from src.ingestion.csv_loader import CSVNewsLoader
from src.ingestion.poller import ConsumerPoller
from src.llm.provider import LLMProvider
from src.market import MarketDataProvider
from src.pipeline.orchestrator import PipelineOrchestrator, WindowConfig
from src.triggers.engine import TriggerEngine


@dataclass
class LLMProviders:
    """LLM 提供者集合"""
    judge: LLMProvider
    deep_analysis: LLMProvider
    risk_control: LLMProvider
    macro: LLMProvider
    reflection: LLMProvider


class WorkflowComponents:
    """工作流核心组件管理器

    职责：
    - 初始化所有核心组件（Clock, Market, Orchestrator, TriggerEngine 等）
    - 根据模式（simulation/realtime）创建不同配置
    - 提供统一的组件访问接口
    """

    def __init__(self, simulation_mode: bool = False) -> None:
        self.simulation_mode = simulation_mode

        # 初始化 KB 客户端
        self.quant = self._create_quant_client()

        # 初始化 LLM 提供者
        self.llm_providers = self._create_llm_providers()

        # 初始化时钟
        self.clock = self._create_clock()

        # 初始化市场数据提供者
        self.market = MarketDataProvider(clock=self.clock, klines_path=settings.klines_path)

        # 初始化数据摄取组件
        self.csv_loader = self._create_csv_loader() if simulation_mode else None
        self.consumer = ConsumerPoller(self.quant, clock=self.clock)

        # 初始化流水线编排器
        self.orchestrator = PipelineOrchestrator(
            self.quant,
            self.market,
            WindowConfig(),
            judge_provider=self.llm_providers.judge,
            deep_analysis_provider=self.llm_providers.deep_analysis,
            risk_control_provider=self.llm_providers.risk_control,
            macro_provider=self.llm_providers.macro,
            reflection_provider=self.llm_providers.reflection,
            clock=self.clock,
        )

        # 初始化调度器
        self.scheduler = Scheduler(self.clock)

        # 触发器引擎稍后初始化（需要回调函数）
        self.trigger_engine: TriggerEngine | None = None

    def _create_quant_client(self) -> QuantClient:
        """创建 KB 量化客户端（支持高并发场景）"""
        from src.utils.http_resilience import create_resilient_httpx_client
        import subprocess

        base_url = settings.kb_api_base_url.rstrip("/")

        # WSL2 IP 自动解析：绕过 Docker Desktop 端口代理，直连容器
        if "localhost" in base_url or "127.0.0.1" in base_url:
            try:
                result = subprocess.run(
                    ["wsl", "-e", "sh", "-c", "hostname -I"],
                    capture_output=True, text=True, timeout=3,
                )
                wsl_ip = result.stdout.strip().split()[0]
                if wsl_ip:
                    base_url = base_url.replace("localhost", wsl_ip).replace("127.0.0.1", wsl_ip)
                    logger.info("WSL2 IP 解析成功: {} -> {}", settings.kb_api_base_url, base_url)
                else:
                    logger.warning("WSL2 IP 解析返回空，降级使用原始地址")
            except Exception as e:
                logger.warning("WSL2 IP 解析失败 ({})，降级使用原始地址", e)

        if base_url.endswith("/api/v1"):
            base_url = base_url[: -len("/api/v1")]

        # 连接池配置：匹配 300 Agent × 每轮 5+ 工具调用的峰值 (~1500 并发)
        # keepalive_expiry 必须 < 服务端 timeout_keep_alive (65s)，留足余量避免僵尸连接
        # max_connections 控制在 Windows 临时端口池 (16384) 内，TIME_WAIT 120s 仍安全
        config = create_resilient_httpx_client(
            max_connections=1200,
            max_keepalive_connections=800,
            keepalive_expiry=30.0,
            enable_http2=False,
        )

        return QuantClient(
            base_url=base_url,
            api_key=settings.kb_api_key,
            limits=config["limits"],
            timeout=config["timeout"],
        )

    def _create_llm_providers(self) -> LLMProviders:
        """创建所有 LLM 提供者"""
        return LLMProviders(
            judge=LLMProvider(
                "deepseek",
                settings.deepseek_model,
                settings.deepseek_api_key,
                settings.deepseek_base_url,
                temperature=0,
                extra_body={"thinking": {"type": "disabled"}},
            ),
            deep_analysis=LLMProvider(
                "deepseek",
                settings.deepseek_model,
                settings.deepseek_api_key,
                settings.deepseek_base_url,
                temperature=0,
                extra_body={"thinking": {"type": "disabled"}},
            ),
            risk_control=LLMProvider(
                "qwen",
                "qwen3.5-flash",
                settings.qwen_api_key,
                settings.qwen_base_url,
            ),
            macro=LLMProvider(
                "qwen",
                "qwen3.5-flash",
                settings.qwen_api_key,
                settings.qwen_base_url,
                extra_body={"enable_thinking": False},
            ),
            reflection=LLMProvider(
                "qwen",
                settings.qwen_model,
                settings.qwen_api_key,
                settings.qwen_base_url,
                extra_body={"enable_thinking": True},
            ),
        )

    def _create_clock(self) -> Clock:
        """创建时钟（模拟/实时）"""
        if self.simulation_mode:
            start_dt = datetime.fromisoformat(settings.simulation_start_time)
            tick_dur = timedelta(minutes=settings.simulation_tick_duration_minutes)
            end_dt: datetime | None = None
            if settings.simulation_end_time:
                end_dt = datetime.fromisoformat(settings.simulation_end_time)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=BEIJING_TZ)
                logger.info("模拟结束时间: {}", end_dt)
            config = TimeConfig(start_time=start_dt, tick_duration=tick_dur, realtime=False, end_time=end_dt)
            logger.info("模拟模式时钟: start={}, tick={}m", start_dt, settings.simulation_tick_duration_minutes)
            if settings.simulation_resume_from_last:
                return Clock.from_checkpoint(config, settings.simulation_checkpoint_path)
            return Clock(config)
        else:
            config = TimeConfig(
                start_time=datetime.now(BEIJING_TZ),
                tick_duration=timedelta(seconds=1),
                realtime=True,
            )
            logger.info("实时模式时钟: start={}", config.start_time)
            return Clock(config)

    def _create_csv_loader(self) -> CSVNewsLoader:
        """创建 CSV 加载器（仅模拟模式）"""
        return CSVNewsLoader(
            settings.simulation_csv_path,
            self.quant,
            self.clock,
            settings.simulation_tick_duration_minutes,
            retention_rate=settings.simulation_retention_rate,
            ingest_to_kb=settings.simulation_ingest_to_kb,
        )

    def set_trigger_engine(self, engine: TriggerEngine) -> None:
        """设置触发器引擎（需要外部回调函数）"""
        self.trigger_engine = engine

    async def shutdown(self) -> None:
        """关闭所有组件"""
        logger.info("正在关闭工作流组件...")
        await self.consumer.stop()
        if self.trigger_engine:
            await self.trigger_engine.stop()
        await self.quant.close()
        logger.info("所有组件已关闭")
