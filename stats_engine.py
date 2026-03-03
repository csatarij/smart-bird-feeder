#!/usr/bin/env python3
"""Statistics engine: SQLite storage, daily summaries, and JSON export.

Records every bird sighting with species, confidence, timestamp, and
image path. Generates daily/weekly/monthly summaries as JSON for the
GitHub data repository and optional dashboard.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from utils import load_config, setup_logging, PROJECT_ROOT


class StatsEngine:
    """Bird sighting statistics backed by SQLite."""

    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging(config)
        self.storage_cfg = config["storage"]

        db_path = PROJECT_ROOT / self.storage_cfg.get("database_path", "data/birds.db")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.stats_dir = PROJECT_ROOT / self.storage_cfg.get("stats_dir", "data/stats")
        self.stats_dir.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        """Create the sightings table if it doesn't exist."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                date TEXT NOT NULL,
                hour INTEGER NOT NULL,
                species TEXT NOT NULL,
                confidence REAL NOT NULL,
                image_path TEXT,
                predictions_json TEXT,
                user_feedback INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_sightings_date ON sightings(date);
            CREATE INDEX IF NOT EXISTS idx_sightings_species ON sightings(species);
            CREATE INDEX IF NOT EXISTS idx_sightings_timestamp ON sightings(timestamp);
        """)
        self.conn.commit()
        self._ensure_feedback_column()

    def _ensure_feedback_column(self):
        """Add user_feedback column to existing databases (migration)."""
        try:
            self.conn.execute("SELECT user_feedback FROM sightings LIMIT 1")
        except sqlite3.OperationalError:
            self.conn.execute(
                "ALTER TABLE sightings ADD COLUMN user_feedback INTEGER"
            )
            self.conn.commit()

    def record_feedback(self, sighting_id: int, is_correct: bool):
        """Record user verification of a classification (1=correct, 0=incorrect)."""
        cursor = self.conn.execute(
            "UPDATE sightings SET user_feedback = ? WHERE id = ?",
            (1 if is_correct else 0, sighting_id),
        )
        self.conn.commit()
        return cursor.rowcount

    def record_sighting(
        self,
        species: str,
        confidence: float,
        image_path: str | None = None,
        predictions: list[dict] | None = None,
    ):
        """Record a single bird sighting."""
        now = datetime.now()
        self.conn.execute(
            """INSERT INTO sightings (timestamp, date, hour, species, confidence,
               image_path, predictions_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                now.isoformat(),
                now.strftime("%Y-%m-%d"),
                now.hour,
                species,
                round(confidence, 4),
                image_path,
                json.dumps(predictions) if predictions else None,
            ),
        )
        self.conn.commit()
        self.logger.info(f"Recorded: {species} ({confidence:.1%})")

    def get_daily_summary(self, date: str | None = None) -> dict:
        """Generate a summary for a given date (default: today)."""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        rows = self.conn.execute(
            "SELECT species, confidence, hour, image_path FROM sightings WHERE date = ?",
            (date,),
        ).fetchall()

        if not rows:
            return {"date": date, "total_sightings": 0, "species": {}}

        species_stats = {}
        for row in rows:
            sp = row["species"]
            if sp not in species_stats:
                species_stats[sp] = {
                    "count": 0,
                    "avg_confidence": 0.0,
                    "hours_seen": [],
                    "best_confidence": 0.0,
                    "best_image": None,
                }
            stats = species_stats[sp]
            stats["count"] += 1
            stats["avg_confidence"] += row["confidence"]
            stats["hours_seen"].append(row["hour"])
            if row["confidence"] > stats["best_confidence"]:
                stats["best_confidence"] = round(row["confidence"], 4)
                stats["best_image"] = row["image_path"]

        # Finalize averages and deduplicate hours
        for sp, stats in species_stats.items():
            stats["avg_confidence"] = round(stats["avg_confidence"] / stats["count"], 4)
            stats["hours_seen"] = sorted(set(stats["hours_seen"]))

        return {
            "date": date,
            "total_sightings": len(rows),
            "unique_species": len(species_stats),
            "species": species_stats,
        }

    def get_all_time_stats(self) -> dict:
        """Generate all-time statistics summary."""
        total = self.conn.execute("SELECT COUNT(*) as c FROM sightings").fetchone()["c"]

        species_counts = self.conn.execute(
            "SELECT species, COUNT(*) as count FROM sightings GROUP BY species ORDER BY count DESC"
        ).fetchall()

        first = self.conn.execute(
            "SELECT MIN(date) as d FROM sightings"
        ).fetchone()["d"]

        active_days = self.conn.execute(
            "SELECT COUNT(DISTINCT date) as d FROM sightings"
        ).fetchone()["d"]

        # Hourly distribution (when are birds most active?)
        hourly = self.conn.execute(
            "SELECT hour, COUNT(*) as count FROM sightings GROUP BY hour ORDER BY hour"
        ).fetchall()

        return {
            "total_sightings": total,
            "unique_species": len(species_counts),
            "first_sighting": first,
            "active_days": active_days,
            "species_ranking": [
                {"species": r["species"], "count": r["count"]} for r in species_counts
            ],
            "hourly_distribution": {r["hour"]: r["count"] for r in hourly},
            "generated_at": datetime.now().isoformat(),
        }

    def get_recent_daily_counts(self, days: int = 14) -> list[dict]:
        """Return per-day sighting counts for the last N days, oldest first."""
        rows = self.conn.execute(
            """SELECT date, COUNT(*) as count FROM sightings
               WHERE date >= date('now', ?)
               GROUP BY date ORDER BY date""",
            (f"-{days} days",),
        ).fetchall()
        return [{"date": r["date"], "count": r["count"]} for r in rows]

    def export_daily_summary(self, date: str | None = None) -> Path:
        """Export daily summary as JSON file."""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        summary = self.get_daily_summary(date)
        output_path = self.stats_dir / f"daily_{date}.json"

        with open(output_path, "w") as f:
            json.dump(summary, f, indent=2)

        self.logger.info(f"Daily summary exported: {output_path.name}")
        return output_path

    def export_all_time_stats(self) -> Path:
        """Export all-time statistics as JSON file."""
        stats = self.get_all_time_stats()
        output_path = self.stats_dir / "all_time_stats.json"

        with open(output_path, "w") as f:
            json.dump(stats, f, indent=2)

        self.logger.info(f"All-time stats exported: {output_path.name}")
        return output_path

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


if __name__ == "__main__":
    """Quick test / manual export."""
    config = load_config()
    stats = StatsEngine(config)
    print(json.dumps(stats.get_all_time_stats(), indent=2))
    stats.export_all_time_stats()
    stats.close()
