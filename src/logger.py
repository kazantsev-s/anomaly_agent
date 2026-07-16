import logging
from pathlib import Path

from config import get_settings


def init_logger():
    settings = get_settings()

    if not settings.logging_enabled:
        return logging.getLogger('logs')

    log_file = Path(__file__).resolve().parent.parent / settings.log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=settings.log_level.upper(),
        format=settings.log_format,
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ],
        force=True
    )

    return logging.getLogger('logs')
