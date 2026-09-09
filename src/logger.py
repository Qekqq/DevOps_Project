import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = PROJECT_ROOT / "logs"
MAX_LOG_BYTES = 5_000_000
LOG_BACKUP_COUNT = 2
_file_handlers: dict[Path, RotatingFileHandler] = {}
_configuration_lock = Lock()


def get_logger(name: str) -> logging.Logger:
    """
    Создаёт и возвращает логгер для модуля проекта.

    Логи выводятся в консоль и logs/<APP_SERVICE>.log (локально — app.log).
    До 5 МБ на файл и две предыдущие копии, как у JSONL-событий.
    Модули одного процесса используют общий обработчик ротации.
    """
    with _configuration_lock:
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        if logger.handlers:
            return logger

        directory = Path(os.getenv("APP_LOG_DIR", str(LOGS_DIR)))
        directory.mkdir(parents=True, exist_ok=True)
        service = os.getenv("APP_SERVICE") or "app"
        path = (directory / f"{service}.log").resolve()
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)

        if path not in _file_handlers:
            file_handler = RotatingFileHandler(
                path,
                maxBytes=MAX_LOG_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
                delay=True,
            )
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(formatter)
            _file_handlers[path] = file_handler

        logger.addHandler(console_handler)
        logger.addHandler(_file_handlers[path])
        logger.propagate = False
        return logger
