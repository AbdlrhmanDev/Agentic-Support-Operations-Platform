"""Versioned prompts. A prompt change is a new file and a new version."""

from dataclasses import dataclass
from functools import cache
from pathlib import Path

_DIR = Path(__file__).parent
CURRENT_VERSION = "support_agent_v2"


@dataclass(frozen=True)
class Prompt:
    version: str
    text: str


@cache
def load_prompt(version: str = CURRENT_VERSION) -> Prompt:
    return Prompt(version, (_DIR / f"{version}.md").read_text(encoding="utf-8").strip())
