import logging

from config import get_settings


def init_logger():
    settings = get_settings()

    logging.basicConfig(
        level=settings.log_level.upper(),
        format=settings.log_format,
    )

    return logging.getLogger("logs")
