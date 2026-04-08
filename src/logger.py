# logger.py
#
# Configures structured logging for the application.
# Outputs to both a rotating file (logs/app.log) and stdout.

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    level = getattr(logging, log_level, logging.INFO)

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(level)

    # Rotating file handler (5 MB × 3 backups)
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(file_handler)

    # Quieten noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("plexapi").setLevel(logging.WARNING)
    logging.getLogger("waitress").setLevel(logging.INFO)

    return logging.getLogger("plex_launcher")
