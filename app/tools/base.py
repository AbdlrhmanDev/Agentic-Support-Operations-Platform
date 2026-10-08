"""The contract every tool follows."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.policies.embeddings import Embedder
from app.services.decisions import Decision


class ToolKind(StrEnum):
    READ = "read"
    WRITE = "write"


class ToolArgs(BaseModel):
    """Base for tool arguments. Unknown fields are rejected, never ignored."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


@dataclass(frozen=True)
class RunScope:
    """Who and what a run may act on. Set by the server, never by the model."""

    run_id: str
    customer_id: str
    ticket_id: str


@dataclass(frozen=True)
class ToolContext:
    scope: RunScope
    session: AsyncSession
    settings: Settings
    embedder: Embedder
    approval_id: str | None = None


# Handlers and deciders take the validated arguments model for their own tool.
# The registry holds tools of every argument type, hence `Any` here.
Handler = Callable[[ToolContext, Any], Awaitable[dict[str, Any]]]
Decider = Callable[[ToolContext, Any, bool], Awaitable[Decision]]
Resolver = Callable[[Any], tuple[str, dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    kind: ToolKind
    args_model: type[ToolArgs]
    handler: Handler
    # Write tools only: rules on whether the call may run. Third argument is
    # True when a human review was asked for.
    decide: Decider | None = None
    # Set on a tool that only asks for review of another tool's action. Maps
    # its arguments to that tool's name and raw arguments.
    resolve: Resolver | None = None

    def __post_init__(self) -> None:
        if self.kind is ToolKind.WRITE and self.decide is None and self.resolve is None:
            raise ValueError(f"Write tool {self.name} must define its decision rule.")
        if self.kind is ToolKind.READ and self.decide is not None:
            raise ValueError(f"Read tool {self.name} cannot define a decision rule.")

    def llm_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.args_model.model_json_schema(),
        }


def idempotency_key(run_id: str, tool: str, payload: dict[str, Any]) -> str:
    """A stable key for one write within one run.

    The same action proposed twice in a run, by a retry, a resumed run or the
    model repeating itself, maps to the same key and so to one side effect.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{run_id}|{tool}|{canonical}".encode()).hexdigest()
