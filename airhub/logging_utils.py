"""Producer logging shared by orchestration and fetchers."""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


LOGGER_NAME = "airhub.producer"


class UtcFormatter(logging.Formatter):
    converter = time.gmtime


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def setup_producer_logging(root: Path, run_id: str | None = None) -> logging.Logger:
    run_id = run_id or new_run_id()
    log_path = root / "logs" / "producer.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = UtcFormatter(
        f"%(asctime)sZ %(levelname)s run={run_id} %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


def get_producer_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
