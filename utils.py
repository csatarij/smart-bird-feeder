"""Shared utilities: config loading, logging, paths."""

import logging
import logging.handlers
import sys
import yaml
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def load_config(config_path: str | None = None) -> dict:
    """Load YAML configuration file."""
    if config_path is None:
        config_path = PROJECT_ROOT / "settings.yaml"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def setup_logging(config: dict) -> logging.Logger:
    """Configure rotating file + console logging."""
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)

    logger = logging.getLogger("bird_feeder")
    logger.setLevel(level)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # File handler (rotating)
    log_file = PROJECT_ROOT / log_cfg.get("file", "data/bird_feeder.log")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=log_cfg.get("max_bytes", 5 * 1024 * 1024),
        backupCount=log_cfg.get("backup_count", 3),
    )
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    return logger


def ensure_directories(config: dict) -> None:
    """Create all data directories if they don't exist."""
    storage = config.get("storage", {})
    for key in ("captures_dir", "classified_dir", "stats_dir"):
        path = PROJECT_ROOT / storage.get(key, f"data/{key}")
        path.mkdir(parents=True, exist_ok=True)


def get_disk_usage_mb(directory: str | Path) -> float:
    """Calculate total disk usage of a directory in MB."""
    total = 0
    directory = Path(directory)
    if directory.exists():
        for f in directory.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    return total / (1024 * 1024)


def prune_old_files(directory: str | Path, max_mb: float) -> int:
    """Delete oldest files until directory is under max_mb. Returns count deleted."""
    directory = Path(directory)
    usage_bytes = sum(
        f.stat().st_size for f in directory.rglob("*") if f.is_file()
    ) if directory.exists() else 0
    max_bytes = max_mb * 1024 * 1024

    if usage_bytes <= max_bytes:
        return 0

    files = sorted(directory.rglob("*.jpg"), key=lambda f: f.stat().st_mtime)
    deleted = 0
    for f in files:
        if usage_bytes <= max_bytes:
            break
        size = f.stat().st_size
        f.unlink()
        usage_bytes -= size
        deleted += 1
    return deleted
