from __future__ import annotations

from pathlib import Path
from typing import Any


_BASE_DIR = Path(__file__).resolve().parents[2]


def load_prompt(relative_path: str) -> str:
    path = _BASE_DIR / relative_path
    return path.read_text(encoding="utf-8")


def format_prompt(relative_path: str, **kwargs: Any) -> str:
    template = load_prompt(relative_path)
    return template.format(**kwargs)
