import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional


SESSION_FORMATTER = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

SESSION_DIR: Optional[Path] = None
ROOT_FILE_HANDLER: Optional[logging.Handler] = None


def _logs_root() -> Path:
    return Path(__file__).resolve().parents[2] / "logs"


def _sanitize(label: str) -> str:
    return "".join(ch for ch in label if ch.isalnum() or ch in "-_")


def _init_session_dir() -> Path:
    global SESSION_DIR
    if SESSION_DIR is not None:
        return SESSION_DIR

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_hint = os.getenv("RUN_LOG_LABEL")
    folder_name = _sanitize(run_hint) if run_hint else f"run_{timestamp}_{os.getpid()}"

    base = _logs_root()
    session_dir = base / folder_name
    session_dir.mkdir(parents=True, exist_ok=True)

    SESSION_DIR = session_dir
    os.environ["RUN_LOG_DIR"] = str(session_dir)
    return session_dir


def _ensure_root_file_handler() -> None:
    global ROOT_FILE_HANDLER
    if ROOT_FILE_HANDLER is not None:
        return

    session_dir = _init_session_dir()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    combined_file = session_dir / "run.log"
    handler = logging.FileHandler(combined_file, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(SESSION_FORMATTER)
    root.addHandler(handler)
    ROOT_FILE_HANDLER = handler


def setup_logger(name: str, log_dir: Optional[str] = None) -> logging.Logger:
    """Configure and return a logger stored under the current run directory."""
    _ensure_root_file_handler()

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if logger.handlers:
        return logger

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(SESSION_FORMATTER)

    target_dir = Path(log_dir) if log_dir else _init_session_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    log_file = target_dir / f"{name}.log"

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(SESSION_FORMATTER)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


# Symbols for structured terminal output
SUCCESS_ICON = "[OK]"
ERROR_ICON = "[X]"
WAIT_ICON = "[..]"
