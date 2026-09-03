import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

def setup_logging(level=logging.INFO, log_file="logs/automation.log"):
    """Configure console + rotating file logging for the whole app.

    Call this once, as early as possible, from main.py / scheduler.py.
    Every module just does `logger = logging.getLogger(__name__)` and logs normally.
    """
    root = logging.getLogger()
    if root.handlers:
        return  # already configured (avoids duplicate handlers on repeated calls)
    root.setLevel(level)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # instagrapi is chatty at DEBUG; keep it at INFO unless someone bumps root below INFO.
    logging.getLogger("instagrapi").setLevel(max(level, logging.INFO))
    logging.getLogger("httpx").setLevel(max(level, logging.WARNING))
    logging.getLogger("urllib3").setLevel(max(level, logging.WARNING))
