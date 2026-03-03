#!/usr/bin/env python3
"""GitHub sync: pushes daily statistics and selected bird photos to a
separate data repository.

Privacy notes:
- Only classified bird crops (never raw frames) are uploaded.
- EXIF is already stripped by the privacy module.
- Stats contain only species/confidence/timestamps — no personal data.
- Uses SSH key authentication (no passwords in config).

Usage:
    python3 github_sync.py [--config settings.yaml]
    # Or via cron: 0 22 * * * cd ~/smart-bird-feeder && python3 github_sync.py
"""

import argparse
import json
import shutil
import subprocess
from datetime import datetime

from stats_engine import StatsEngine
from utils import PROJECT_ROOT, load_config, setup_logging


class GitHubSync:
    """Manages syncing bird data to a GitHub repository."""

    def __init__(self, config: dict):
        self.config = config
        self.gh_cfg = config.get("github", {})
        self.storage_cfg = config["storage"]
        self.logger = setup_logging(config)

        self.data_repo_dir = PROJECT_ROOT / "github_data_repo"
        self.classified_dir = PROJECT_ROOT / self.storage_cfg["classified_dir"]
        self.stats_dir = PROJECT_ROOT / self.storage_cfg["stats_dir"]

    def _run_git(self, *args, cwd=None) -> tuple[bool, str]:
        """Run a git command and return (success, output)."""
        cmd = ["git"] + list(args)
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd or str(self.data_repo_dir),
                capture_output=True,
                text=True,
                timeout=120,
            )
            return result.returncode == 0, result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return False, "Git command timed out"
        except Exception as e:
            return False, str(e)

    def setup_repo(self) -> bool:
        """Clone or initialize the data repository."""
        if not self.gh_cfg.get("enabled", False):
            self.logger.warning("GitHub sync is disabled in config")
            return False

        repo_url = self.gh_cfg.get("repo_url", "")
        if not repo_url:
            self.logger.error("No repo_url configured")
            return False

        if (self.data_repo_dir / ".git").exists():
            # Pull latest
            ok, out = self._run_git("pull", "--rebase")
            if not ok:
                self.logger.warning(f"Git pull failed: {out}")
                return False
            return True

        # Clone the repo
        self.data_repo_dir.mkdir(parents=True, exist_ok=True)
        ok, out = self._run_git(
            "clone",
            repo_url,
            str(self.data_repo_dir),
            cwd=str(PROJECT_ROOT),
        )
        if not ok:
            self.logger.error(f"Git clone failed: {out}")
            return False

        self.logger.info("Data repository cloned successfully")
        return True

    def sync(self) -> bool:
        """Perform a full sync: export stats, copy photos, commit, push."""
        if not self.gh_cfg.get("enabled", False):
            self.logger.info("GitHub sync disabled — skipping")
            return False

        if not self.setup_repo():
            return False

        today = datetime.now().strftime("%Y-%m-%d")
        sightings_count = 0

        # 1. Export fresh statistics
        stats = StatsEngine(self.config)
        stats.export_daily_summary()
        stats.export_all_time_stats()
        stats.close()

        # 2. Copy stats to data repo
        repo_stats_dir = self.data_repo_dir / "stats"
        repo_stats_dir.mkdir(parents=True, exist_ok=True)

        for json_file in self.stats_dir.glob("*.json"):
            shutil.copy2(str(json_file), str(repo_stats_dir / json_file.name))

        # 3. Copy selected bird photos (up to daily limit)
        if self.gh_cfg.get("sync_photos", True):
            max_uploads = self.gh_cfg.get("max_daily_uploads", 20)
            repo_photos_dir = self.data_repo_dir / "photos" / today
            repo_photos_dir.mkdir(parents=True, exist_ok=True)

            uploaded = 0
            for species_dir in sorted(self.classified_dir.iterdir()):
                if not species_dir.is_dir() or species_dir.name.startswith("_"):
                    continue
                for photo in sorted(species_dir.glob("*.jpg"))[: max_uploads - uploaded]:
                    dest_dir = repo_photos_dir / species_dir.name
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(photo), str(dest_dir / photo.name))
                    uploaded += 1
                    sightings_count += 1
                    if uploaded >= max_uploads:
                        break
                if uploaded >= max_uploads:
                    break

            self.logger.info(f"Copied {uploaded} photos to data repo")

        # 4. Generate a README for the data repo
        self._update_data_readme()

        # 5. Git add, commit, push
        self._run_git("add", "-A")

        commit_msg = self.gh_cfg.get("commit_template", "Daily update: {date}").format(
            date=today, count=sightings_count
        )
        ok, out = self._run_git("commit", "-m", commit_msg)
        if not ok:
            if "nothing to commit" in out:
                self.logger.info("No changes to commit")
                return True
            self.logger.error(f"Git commit failed: {out}")
            return False

        branch = self.gh_cfg.get("branch", "main")
        ok, out = self._run_git("push", "origin", branch)
        if not ok:
            self.logger.error(f"Git push failed: {out}")
            return False

        self.logger.info(f"Successfully synced to GitHub ({commit_msg})")
        return True

    def _update_data_readme(self):
        """Generate a README.md for the data repository with latest stats."""
        stats_file = self.stats_dir / "all_time_stats.json"
        if not stats_file.exists():
            return

        with open(stats_file) as f:
            stats = json.load(f)

        readme = f"""# Bird Feeder Data

Auto-generated by [Smart Bird Feeder](https://github.com/YOUR_USERNAME/smart-bird-feeder).

## Summary

| Metric | Value |
|--------|-------|
| Total sightings | {stats.get("total_sightings", 0)} |
| Unique species | {stats.get("unique_species", 0)} |
| Monitoring since | {stats.get("first_sighting", "N/A")} |
| Active days | {stats.get("active_days", 0)} |

## Species Observed

| Rank | Species | Sightings |
|------|---------|-----------|
"""
        for i, sp in enumerate(stats.get("species_ranking", [])[:20], 1):
            readme += f"| {i} | {sp['species']} | {sp['count']} |\n"

        readme += f"""
## Hourly Activity

Peak bird activity times based on all observations.

---

*Last updated: {stats.get("generated_at", "unknown")}*
*All data collected offline on a Raspberry Pi 1B. No cloud services used.*
"""
        readme_path = self.data_repo_dir / "README.md"
        with open(readme_path, "w") as f:
            f.write(readme)


def main():
    parser = argparse.ArgumentParser(description="Sync bird data to GitHub")
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args()
    config = load_config(args.config)
    syncer = GitHubSync(config)
    syncer.sync()


if __name__ == "__main__":
    main()
