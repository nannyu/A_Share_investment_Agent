import json
import os
from datetime import datetime, UTC
from typing import Any, Iterable, Optional


def _ensure_dir(path: str) -> None:
    if not path:
        return
    os.makedirs(path, exist_ok=True)


def _safe_json_dump(path: str, payload: Any) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _append_jsonl(path: str, payload: Any) -> None:
    _ensure_dir(os.path.dirname(path))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def trace_agent_io(
    trace_dir: Optional[str],
    agent_name: str,
    input_state: Any,
    output_state: Any,
    terminal_outputs: Optional[Iterable[str]] = None,
    reasoning_details: Optional[str] = None,
) -> None:
    if not trace_dir:
        return
    agent_dir = os.path.join(trace_dir, agent_name)
    _safe_json_dump(os.path.join(agent_dir, "input.json"), input_state)
    _safe_json_dump(os.path.join(agent_dir, "output.json"), output_state)
    if reasoning_details:
        _safe_json_dump(os.path.join(agent_dir, "reasoning.json"), {"reasoning": reasoning_details})
    if terminal_outputs:
        _safe_json_dump(os.path.join(agent_dir, "terminal_outputs.json"), list(terminal_outputs))


def trace_llm_interaction(
    trace_dir: Optional[str],
    agent_name: str,
    request_data: Any,
    response_data: Any,
) -> None:
    if not trace_dir:
        return
    llm_path = os.path.join(trace_dir, agent_name, "llm.jsonl")
    _append_jsonl(
        llm_path,
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "agent_name": agent_name,
            "request": request_data,
            "response": response_data,
        },
    )
