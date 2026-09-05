"""工具包 —— 所有 Agent 工具（装饰器注册 + ToolRegistry）"""

from __future__ import annotations

from typing import Any

# -- re-export all tool modules (triggers @register_tool side-effects) ----
from . import (
    knowledge,  # noqa: F401  (triggers @register_tool side-effects)
    macro,  # noqa: F401  (triggers @register_tool side-effects)
    review,  # noqa: F401  (triggers @register_tool side-effects)
    writer,  # noqa: F401  (triggers @register_tool side-effects)
)
from . import (
    market as _market_mod,  # noqa: F401  (triggers @register_tool side-effects)
)
from ._deps import (
    get_registry_items as get_registry_items,
)
from ._deps import (
    get_session_registry as get_session_registry,
)

# -- core infrastructure (only re-export what external callers actually use) --
from ._deps import (
    mark_registry_source as mark_registry_source,
)
from ._deps import (
    register_entity as register_entity,
)
from ._deps import (
    reset_session_registry as reset_session_registry,
)
from ._deps import (
    set_task_context as set_task_context,
)
from .context import init_ctx as init_ctx


# -- deprecated compatibility alias ---------------------------------------
# Remove once all callers have been migrated to init_ctx().
def init_tool_deps(quant: Any = None, market: Any = None, compiler: Any = None, clock: Any = None) -> None:
    """Deprecated. Use init_ctx(ToolContext(...)) instead."""
    return init_ctx(quant=quant, market=market, compiler=compiler, clock=clock)  # noqa: F811
