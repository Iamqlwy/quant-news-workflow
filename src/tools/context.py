"""ToolContext — single contextvar replacing 5 scattered dependency contextvars."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kbquant.client import QuantClient

    from src.core.clock import Clock
    from src.market import MarketDataProvider
    from src.triggers.compiler import TriggerCompiler


@dataclass
class ToolContext:
    """All dependencies a tool coroutine might need. Injected once before agent.run()."""

    quant: QuantClient
    market: MarketDataProvider
    compiler: TriggerCompiler | None = None
    clock: Clock | None = None


_ctx_var: contextvars.ContextVar[ToolContext | None] = contextvars.ContextVar("tool_ctx", default=None)


def init_ctx(
    quant: QuantClient,
    market: MarketDataProvider,
    compiler: TriggerCompiler | None = None,
    clock: Clock | None = None,
) -> ToolContext:
    """Inject dependencies into the tool contextvar. Call before agent.run().

    Replaces the old init_tool_deps() which wrote 5 separate contextvars.
    """
    ctx = ToolContext(
        quant=quant,
        market=market,
        compiler=compiler,
        clock=clock,
    )
    _ctx_var.set(ctx)
    return ctx


def get_ctx() -> ToolContext:
    """Retrieve the current ToolContext. Raises RuntimeError if uninitialized."""
    ctx = _ctx_var.get()
    if ctx is None:
        raise RuntimeError("ToolContext is not initialized — call init_ctx() first")
    return ctx
