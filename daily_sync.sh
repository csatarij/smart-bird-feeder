#!/bin/bash
# Daily GitHub sync — add to crontab:
# 0 22 * * * /home/pi/smart-bird-feeder/daily_sync.sh >> /home/pi/smart-bird-feeder/data/sync.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"

cd "$PROJECT_DIR"
source "$PROJECT_DIR/venv/bin/activate"

echo "--- Sync started: $(date) ---"

# Export fresh stats
python3 -c "
from src.stats_engine import StatsEngine
from src.utils import load_config
config = load_config()
stats = StatsEngine(config)
stats.export_daily_summary()
stats.export_all_time_stats()
stats.close()
print('Stats exported successfully')
"

# Push to GitHub
python3 src/github_sync.py

echo "--- Sync complete: $(date) ---"
