"""ToolRegistry — decorator-based registration with category & name indexing."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from src.observability import wrap_tool_coroutine


class ValidatedStructuredTool(StructuredTool):
    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        if isinstance(input, dict) and self.coroutine is not None:
            return await self.coroutine(**input)
        return await super().ainvoke(input, config=config, **kwargs)


class ToolRegistry:
    """Global registry mapping tool name -> StructuredTool, with category indexing."""

    def __init__(self) -> None:
        self._tools: dict[str, StructuredTool] = {}
        self._by_category: dict[str, list[str]] = {}
        self._coroutines: dict[str, Callable] = {}

    def register(
        self,
        name: str,
        description: str,
        category: str,
        args_schema: type[BaseModel],
        coroutine: Callable,
    ) -> StructuredTool:
        allowed_keys = set(inspect.signature(coroutine).parameters.keys())

        async def validated_coroutine(**kwargs: Any) -> Any:
            parsed = args_schema.model_validate(kwargs)
            normalized_kwargs = {
                key: value
                for key, value in parsed.model_dump(exclude_none=True).items()
                if key in allowed_keys
            }
            return await coroutine(**normalized_kwargs)

        traced_coroutine = wrap_tool_coroutine(
            name=name,
            category=category,
            coroutine=validated_coroutine,
        )
        tool = ValidatedStructuredTool(
            name=name,
            description=description,
            args_schema=args_schema,
            coroutine=traced_coroutine,
        )
        self._tools[name] = tool
        self._by_category.setdefault(category, []).append(name)
        self._coroutines[name] = coroutine
        return tool

    # -- queries -------------------------------------------------------

    def get(self, *names: str) -> list[StructuredTool]:
        """Return tools by exact name, in the order given."""
        result: list[StructuredTool] = []
        for n in names:
            t = self._tools.get(n)
            if t is None:
                raise KeyError(f"Tool '{n}' is not registered")
            result.append(t)
        return result

    def get_tools(self, *categories: str) -> list[StructuredTool]:
        """Return every tool whose category matches (union)."""
        if not categories:
            return list(self._tools.values())
        seen: set[str] = set()
        result: list[StructuredTool] = []
        for cat in categories:
            for name in self._by_category.get(cat, []):
                if name not in seen:
                    seen.add(name)
                    result.append(self._tools[name])
        return result

    def get_all(self) -> list[StructuredTool]:
        """Return every registered tool."""
        return list(self._tools.values())

    def get_coroutine(self, name: str) -> Callable:
        """Return the original async function (for direct testing)."""
        c = self._coroutines.get(name)
        if c is None:
            raise KeyError(f"Tool '{name}' is not registered")
        return c

    def list_names(self, category: str | None = None) -> list[str]:
        if category:
            return list(self._by_category.get(category, []))
        return list(self._tools.keys())


# -- global singleton ----------------------------------------------------

_registry = ToolRegistry()


def register_tool(
    name: str,
    description: str,
    category: str,
    args_schema: type[BaseModel],
) -> Callable[[Callable], StructuredTool]:
    """Decorator: wraps an async function into a StructuredTool and registers it.

    Usage::

        class SearchKBArgs(BaseModel):
            query_text: str = Field(...)

        @register_tool(
            name="search_kb",
            description="...",
            category="knowledge",
            args_schema=SearchKBArgs,
        )
        async def search_kb(query_text: str) -> str: ...
    """

    def decorator(func: Callable) -> StructuredTool:
        return _registry.register(name, description, category, args_schema, func)

    return decorator


# -- module-level convenience re-exports --------------------------------


def get_tools(*categories: str) -> list[StructuredTool]:
    return _registry.get_tools(*categories)


def get(*names: str) -> list[StructuredTool]:
    return _registry.get(*names)


def get_all() -> list[StructuredTool]:
    return _registry.get_all()


def get_coroutine(name: str) -> Callable:
    return _registry.get_coroutine(name)
