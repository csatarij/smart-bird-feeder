"""Shared utilities: config loading, logging, paths."""

import copy
import logging
import logging.handlers
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent
LOCAL_CONFIG_PATH = PROJECT_ROOT / "settings.local.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Override values win."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(config_path: str | None = None) -> dict:
    """Load YAML configuration, merging settings.local.yaml overrides on top.

    Merge order: settings.yaml (defaults, git-tracked) -> settings.local.yaml (user
    overrides, gitignored). Local values win at any nesting depth.
    """
    if config_path is None:
        config_path = PROJECT_ROOT / "settings.yaml"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    # Merge local overrides if they exist
    if LOCAL_CONFIG_PATH.exists():
        with open(LOCAL_CONFIG_PATH, "r") as f:
            local = yaml.safe_load(f)
        if local and isinstance(local, dict):
            config = _deep_merge(config, local)

    return config


def save_local_config(overrides: dict) -> Path:
    """Save user overrides to settings.local.yaml (gitignored).

    Merges new overrides into any existing local config so that previously
    saved values are preserved unless explicitly replaced.
    """
    existing = {}
    if LOCAL_CONFIG_PATH.exists():
        with open(LOCAL_CONFIG_PATH, "r") as f:
            existing = yaml.safe_load(f) or {}

    merged = _deep_merge(existing, overrides)
    with open(LOCAL_CONFIG_PATH, "w") as f:
        yaml.dump(merged, f, default_flow_style=False, sort_keys=False)
    return LOCAL_CONFIG_PATH


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

    rotation = log_cfg.get("rotation", "size")
    backup_count = log_cfg.get("backup_count", 30)

    if rotation == "daily":
        fh = logging.handlers.TimedRotatingFileHandler(
            log_file,
            when="midnight",
            backupCount=backup_count,
            encoding="utf-8",
        )
    else:
        fh = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=log_cfg.get("max_bytes", 5 * 1024 * 1024),
            backupCount=backup_count,
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
    usage_bytes = (
        sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
        if directory.exists()
        else 0
    )
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
