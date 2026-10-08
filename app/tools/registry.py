from typing import Any

from app.tools.base import ToolSpec
from app.tools.read import READ_TOOLS
from app.tools.write import WRITE_TOOLS


class ToolRegistry:
    """The allowlist of tools the model can call. Nothing outside it can run."""

    def __init__(self, tools: list[ToolSpec]) -> None:
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("Tool names must be unique.")

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def llm_schemas(self) -> list[dict[str, Any]]:
        # Fixed order: the tool list is part of the cached prompt prefix.
        return [tool.llm_schema() for tool in self._tools.values()]


def default_registry() -> ToolRegistry:
    return ToolRegistry([*READ_TOOLS, *WRITE_TOOLS])
