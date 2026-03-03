#!/usr/bin/env python3
"""Local network web server for browsing bird photos and system health.

Runs on the Pi and serves:
  /                  — Photo gallery (browse by species, date)
  /stats             — Capture statistics dashboard
  /calibration       — Confidence calibration & sightings inspector
  /health            — System health dashboard
  /api/stats         — JSON stats endpoint
  /api/calibration   — JSON calibration/debug data
  /api/health        — JSON health data
  /api/feedback      — POST endpoint for marking sightings correct/incorrect
  /photos/<path>     — Classified bird photos

Uses only the Python standard library + project dependencies (no Flask needed).

Usage:
    python3 webserver.py [--config settings.yaml] [--port 8080]
"""

import argparse
import json
import mimetypes
import shutil
import socketserver
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote

from utils import PROJECT_ROOT, load_config, setup_logging

# Resolved once at startup
CONFIG = None
LOGGER = None
DATA_DIR = None
CLASSIFIED_DIR = None
STATS_DIR = None
DB_PATH = None
POWER_CFG = None  # power_monitoring section of config


def _ensure_feedback_column(conn):
    """Add user_feedback column to existing databases (migration for the webserver)."""
    try:
        conn.execute("SELECT user_feedback FROM sightings LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE sightings ADD COLUMN user_feedback INTEGER")
        conn.commit()


def _image_path_to_url(image_path: str | None) -> str | None:
    """Convert a DB image_path to a /photos/... URL, or None if not resolvable."""
    if not image_path:
        return None
    p = Path(image_path)
    # Try to make path relative to the classified dir
    try:
        rel = p.resolve().relative_to(CLASSIFIED_DIR.resolve())
        return f"/photos/{rel.as_posix()}"
    except ValueError:
        pass
    # Search for 'classified' segment in the path parts
    parts = p.parts
    for i, part in enumerate(parts):
        if part == "classified" and i + 1 < len(parts):
            return "/photos/" + "/".join(parts[i + 1 :])
    # Last resort: assume the last two parts are <species>/<filename>
    if len(parts) >= 2:
        return f"/photos/{parts[-2]}/{parts[-1]}"
    return None


def get_power_metrics() -> dict:
    """Read CPU temperature and core voltage via vcgencmd (or /sys fallback).

    Returns a dict with keys cpu_temp_c, core_volts_v, throttled.
    Any value may be None if the source is unavailable (non-Pi hardware,
    vcgencmd not on PATH, or permission error).
    """
    metrics: dict = {"cpu_temp_c": None, "core_volts_v": None, "throttled": None}

    # CPU temperature — vcgencmd first, /sys fallback
    try:
        out = subprocess.check_output(
            ["vcgencmd", "measure_temp"], timeout=3, stderr=subprocess.DEVNULL, text=True
        )
        # "temp=45.0'C"
        metrics["cpu_temp_c"] = round(float(out.strip().replace("temp=", "").replace("'C", "")), 1)
    except Exception:
        pass

    if metrics["cpu_temp_c"] is None:
        try:
            raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
            metrics["cpu_temp_c"] = round(int(raw) / 1000.0, 1)
        except Exception:
            pass

    # Core voltage
    try:
        out = subprocess.check_output(
            ["vcgencmd", "measure_volts", "core"],
            timeout=3,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # "volt=1.3500V"
        metrics["core_volts_v"] = round(float(out.strip().replace("volt=", "").replace("V", "")), 4)
    except Exception:
        pass

    # Throttle / under-voltage flags
    try:
        out = subprocess.check_output(
            ["vcgencmd", "get_throttled"],
            timeout=3,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # "throttled=0x0"
        metrics["throttled"] = out.strip().replace("throttled=", "")
    except Exception:
        pass

    return metrics


def _read_power_history(max_rows: int = 288) -> list[dict]:
    """Read the most recent rows from the power log CSV.

    max_rows=288 covers 24 h at 5-minute intervals.
    """
    if POWER_CFG is None:
        return []
    log_file = PROJECT_ROOT / POWER_CFG.get("log_file", "data/power_log.csv")
    if not log_file.exists():
        return []
    try:
        lines = log_file.read_text().splitlines()
        data_lines = [ln for ln in lines[1:] if ln.strip()]  # skip header
        rows = []
        for line in data_lines[-max_rows:]:
            parts = line.split(",")
            if len(parts) >= 3:
                rows.append(
                    {
                        "ts": parts[0],
                        "cpu_temp_c": float(parts[1]) if parts[1] else None,
                        "core_volts_v": float(parts[2]) if parts[2] else None,
                        "throttled": parts[3].strip() if len(parts) > 3 else None,
                    }
                )
        return rows
    except Exception:
        return []


def _power_log_worker():
    """Background daemon thread: append a CSV row every interval_seconds."""
    if POWER_CFG is None or not POWER_CFG.get("enabled", False):
        return

    interval = POWER_CFG.get("interval_seconds", 300)
    log_file = PROJECT_ROOT / POWER_CFG.get("log_file", "data/power_log.csv")
    log_file.parent.mkdir(parents=True, exist_ok=True)

    if not log_file.exists() or log_file.stat().st_size == 0:
        log_file.write_text("timestamp,cpu_temp_c,core_volts_v,throttled\n")

    while True:
        try:
            m = get_power_metrics()
            ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            temp = "" if m["cpu_temp_c"] is None else str(m["cpu_temp_c"])
            volts = "" if m["core_volts_v"] is None else str(m["core_volts_v"])
            throttled = "" if m["throttled"] is None else m["throttled"]
            with open(log_file, "a") as f:
                f.write(f"{ts},{temp},{volts},{throttled}\n")
        except Exception:
            pass  # Never crash the background thread
        time.sleep(interval)


def get_health_data() -> dict:
    """Gather system health metrics."""
    disk = shutil.disk_usage(str(DATA_DIR))
    disk_total_gb = disk.total / (1024**3)
    disk_used_gb = disk.used / (1024**3)
    disk_free_gb = disk.free / (1024**3)
    disk_pct = (disk.used / disk.total) * 100

    # Count photos
    captures_dir = DATA_DIR / "captures"
    queue_count = len(list(captures_dir.glob("*.jpg"))) if captures_dir.exists() else 0

    classified_count = 0
    species_dirs = []
    if CLASSIFIED_DIR.exists():
        for d in sorted(CLASSIFIED_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith("_"):
                count = len(list(d.glob("*.jpg")))
                classified_count += count
                species_dirs.append({"name": d.name, "count": count})

    unclassified_dir = CLASSIFIED_DIR / "_unclassified"
    unclassified_count = (
        len(list(unclassified_dir.glob("*.jpg"))) if unclassified_dir.exists() else 0
    )

    # DB stats
    db_stats = {}
    db_size_mb = 0
    if DB_PATH.exists():
        db_size_mb = DB_PATH.stat().st_size / (1024 * 1024)
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            total = conn.execute("SELECT COUNT(*) as c FROM sightings").fetchone()["c"]
            unique = conn.execute("SELECT COUNT(DISTINCT species) as c FROM sightings").fetchone()[
                "c"
            ]
            today = datetime.now().strftime("%Y-%m-%d")
            today_count = conn.execute(
                "SELECT COUNT(*) as c FROM sightings WHERE date = ?", (today,)
            ).fetchone()["c"]
            latest = conn.execute(
                "SELECT timestamp, species, confidence FROM sightings "
                "ORDER BY timestamp DESC LIMIT 5"
            ).fetchall()
            db_stats = {
                "total_sightings": total,
                "unique_species": unique,
                "today_sightings": today_count,
                "recent": [
                    {
                        "timestamp": r["timestamp"],
                        "species": r["species"],
                        "confidence": round(r["confidence"], 3),
                    }
                    for r in latest
                ],
            }
            conn.close()
        except Exception:
            db_stats = {"error": "Could not read database"}

    # Log file info
    log_path = DATA_DIR / "bird_feeder.log"
    log_size_mb = log_path.stat().st_size / (1024 * 1024) if log_path.exists() else 0

    # Process uptime check (best-effort via PID files or /proc)
    capture_running = _check_process("motion_detector")
    classify_running = _check_process("classifier")

    # Power & thermal metrics
    power_current = get_power_metrics()
    history_hours = (POWER_CFG or {}).get("history_hours", 24)
    max_rows = int(history_hours * 3600 / max((POWER_CFG or {}).get("interval_seconds", 300), 1))
    power_history = _read_power_history(max_rows)

    return {
        "timestamp": datetime.now().isoformat(),
        "disk": {
            "total_gb": round(disk_total_gb, 1),
            "used_gb": round(disk_used_gb, 1),
            "free_gb": round(disk_free_gb, 1),
            "used_pct": round(disk_pct, 1),
        },
        "photos": {
            "in_queue": queue_count,
            "classified": classified_count,
            "unclassified": unclassified_count,
            "species": species_dirs,
        },
        "database": db_stats,
        "database_size_mb": round(db_size_mb, 2),
        "log_size_mb": round(log_size_mb, 2),
        "processes": {
            "motion_detector": capture_running,
            "classifier": classify_running,
        },
        "power": {
            "current": power_current,
            "history": power_history,
        },
    }


def _check_process(name: str) -> str:
    """Check if a process with the given script name is running."""
    try:
        proc_dir = Path("/proc")
        for pid_dir in proc_dir.iterdir():
            if not pid_dir.name.isdigit():
                continue
            cmdline_file = pid_dir / "cmdline"
            try:
                cmdline = cmdline_file.read_text()
                if name + ".py" in cmdline:
                    return "running"
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
        return "stopped"
    except Exception:
        return "unknown"


def build_gallery_html() -> str:
    """Build the photo gallery HTML page."""
    species_list = []
    if CLASSIFIED_DIR.exists():
        for d in sorted(CLASSIFIED_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith("_"):
                photos = sorted(d.glob("*.jpg"), key=lambda f: f.stat().st_mtime, reverse=True)
                species_list.append(
                    {
                        "name": d.name.replace("_", " ").title(),
                        "dir": d.name,
                        "count": len(photos),
                        "recent": [p.name for p in photos[:12]],
                    }
                )

    species_cards = ""
    for sp in species_list:
        thumbs = ""
        for photo in sp["recent"]:
            thumbs += (
                f'<a href="/photos/{sp["dir"]}/{photo}" target="_blank">'
                f'<img src="/photos/{sp["dir"]}/{photo}" loading="lazy" '
                f'alt="{sp["name"]}">'
                f"</a>\n"
            )
        species_cards += f"""
        <div class="species-card">
            <h2>{sp["name"]} <span class="badge">{sp["count"]}</span></h2>
            <div class="photo-grid">{thumbs}</div>
        </div>
        """

    total = sum(s["count"] for s in species_list)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bird Feeder Photos</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           background: #f5f5f5; color: #333; }}
    nav {{ background: #2e7d32; color: white; padding: 1rem 2rem; display: flex;
           justify-content: space-between; align-items: center; }}
    nav a {{ color: white; text-decoration: none; margin-left: 1.5rem; opacity: 0.85; }}
    nav a:hover {{ opacity: 1; }}
    .container {{ max-width: 1200px; margin: 0 auto; padding: 1.5rem; }}
    .summary {{ background: white; border-radius: 8px; padding: 1rem 1.5rem;
                margin-bottom: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
    .species-card {{ background: white; border-radius: 8px; padding: 1.5rem;
                     margin-bottom: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
    .species-card h2 {{ margin-bottom: 1rem; color: #2e7d32; }}
    .badge {{ background: #e8f5e9; color: #2e7d32; font-size: 0.8rem;
              padding: 2px 8px; border-radius: 12px; vertical-align: middle; }}
    .photo-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
                   gap: 8px; }}
    .photo-grid img {{ width: 100%; aspect-ratio: 1; object-fit: cover;
                       border-radius: 4px; cursor: pointer; transition: transform 0.2s; }}
    .photo-grid img:hover {{ transform: scale(1.05); }}
    .empty {{ text-align: center; padding: 3rem; color: #888; }}
</style>
</head>
<body>
<nav>
    <strong>Bird Feeder</strong>
    <div>
        <a href="/">Photos</a>
        <a href="/stats">Statistics</a>
        <a href="/calibration">Calibration</a>
        <a href="/health">Health</a>
    </div>
</nav>
<div class="container">
    <div class="summary">
        <strong>{len(species_list)}</strong> species &middot;
        <strong>{total}</strong> classified photos
    </div>
    {species_cards if species_cards else '<div class="empty">No classified photos yet. The classifier will populate this page as birds are detected.</div>'}
</div>
</body>
</html>"""


def build_stats_html() -> str:
    """Build the capture statistics HTML page."""
    return STATS_HTML_TEMPLATE


STATS_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bird Feeder - Capture Statistics</title>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           background: #f5f5f5; color: #333; }
    nav { background: #2e7d32; color: white; padding: 1rem 2rem; display: flex;
          justify-content: space-between; align-items: center; }
    nav a { color: white; text-decoration: none; margin-left: 1.5rem; opacity: 0.85; }
    nav a:hover { opacity: 1; }
    nav a.active { opacity: 1; border-bottom: 2px solid white; padding-bottom: 2px; }
    .container { max-width: 960px; margin: 0 auto; padding: 1.5rem; }
    .card { background: white; border-radius: 8px; padding: 1.5rem;
            margin-bottom: 1rem; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
    .card h2 { color: #2e7d32; margin-bottom: 1.2rem; font-size: 1.1rem; }
    /* Summary grid */
    .grid-5 { display: grid; grid-template-columns: repeat(5, 1fr); gap: 0.75rem; }
    @media (max-width: 600px) { .grid-5 { grid-template-columns: repeat(3, 1fr); } }
    .metric { text-align: center; padding: 0.85rem 0.5rem; background: #f9f9f9; border-radius: 6px; }
    .metric .value { font-size: 1.75rem; font-weight: bold; color: #2e7d32; }
    .metric .label { font-size: 0.8rem; color: #666; margin-top: 0.25rem; }
    .meta-row { margin-top: 0.85rem; font-size: 0.82rem; color: #777;
                display: flex; flex-wrap: wrap; gap: 0.5rem 1.25rem; }
    /* Species ranking */
    .species-row { display: flex; align-items: center; margin-bottom: 0.5rem; gap: 0.6rem; }
    .species-thumb { width: 44px; height: 44px; object-fit: cover; border-radius: 4px;
                     flex-shrink: 0; transition: transform 0.15s; display: block; }
    .species-thumb:hover { transform: scale(1.08); }
    .species-thumb-ph { width: 44px; height: 44px; background: #e8f5e9;
                        border-radius: 4px; flex-shrink: 0; }
    .species-name { min-width: 110px; font-size: 0.88rem; text-transform: capitalize; }
    .bar-track { flex: 1; background: #e8f5e9; border-radius: 4px; height: 20px; overflow: hidden; }
    .bar-fill { height: 100%; background: #4caf50; border-radius: 4px;
                transition: width 0.4s ease; display: flex; align-items: center;
                padding-left: 5px; min-width: 2px; }
    .bar-count { font-size: 0.78rem; color: white; font-weight: 600; white-space: nowrap; }
    .bar-count-out { font-size: 0.78rem; color: #555; margin-left: 5px; min-width: 1.5rem; }
    /* Recent captures grid */
    .capture-grid { display: grid;
                    grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
                    gap: 8px; }
    .capture-cell { text-decoration: none; color: inherit; display: block;
                    border-radius: 6px; overflow: hidden; background: #f9f9f9;
                    transition: transform 0.15s; }
    .capture-cell:hover { transform: scale(1.03); }
    .capture-cell img { width: 100%; aspect-ratio: 1; object-fit: cover; display: block; }
    .capture-label { padding: 4px 5px; font-size: 0.72rem; line-height: 1.35; }
    .capture-label strong { display: block; text-transform: capitalize; color: #2e7d32; }
    /* Charts */
    .chart { display: flex; align-items: flex-end; gap: 3px; height: 110px; padding-top: 0.5rem; }
    .chart-col { display: flex; flex-direction: column; align-items: center;
                 flex: 1; height: 100%; justify-content: flex-end; }
    .chart-bar { width: 100%; background: #81c784; border-radius: 3px 3px 0 0;
                 min-height: 0; transition: height 0.4s ease; }
    .chart-bar.peak { background: #2e7d32; }
    .chart-bar:hover { opacity: 0.8; }
    .chart-label { font-size: 0.62rem; color: #888; margin-top: 3px; text-align: center; }
    .chart-value { font-size: 0.62rem; color: #555; margin-bottom: 1px; min-height: 10px; }
    .chart-col:hover .chart-value { font-weight: 600; }
    .empty { color: #999; font-size: 0.9rem; padding: 0.5rem 0; }
    .refresh-info { text-align: center; color: #999; font-size: 0.8rem; margin-top: 1rem; }
    .error { color: #f44336; }
</style>
</head>
<body>
<nav>
    <strong>Bird Feeder</strong>
    <div>
        <a href="/">Photos</a>
        <a href="/stats" class="active">Statistics</a>
        <a href="/calibration">Calibration</a>
        <a href="/health">Health</a>
    </div>
</nav>
<div class="container">
    <div id="content">Loading statistics...</div>
    <div class="refresh-info">Auto-refreshes every 60 seconds</div>
</div>
<script>
function fmtHour(h) {
    if (h === null || h === undefined) return '\u2014';
    const ampm = h < 12 ? 'am' : 'pm';
    const h12 = h === 0 ? 12 : h > 12 ? h - 12 : h;
    return String(h).padStart(2, '0') + ':00\u2009(' + h12 + ampm + ')';
}
function fmtTs(ts) { return ts ? ts.replace('T', ' ').slice(0, 16) : ''; }

function render(d) {
    if (d.error) {
        document.getElementById('content').innerHTML =
            '<div class="card error">' + d.error + '</div>';
        return;
    }

    // ── Summary ──────────────────────────────────────────────────────────────
    const madText = d.most_active_day
        ? d.most_active_day.date + ' (' + d.most_active_day.count + ' captures)'
        : '\u2014';
    const metaParts = [];
    if (d.first_sighting) metaParts.push('First sighting: <strong>' + d.first_sighting + '</strong>');
    metaParts.push('Peak hour: <strong>' + fmtHour(d.peak_hour) + '</strong>');
    metaParts.push('Busiest day: <strong>' + madText + '</strong>');

    // ── Species ranking ───────────────────────────────────────────────────────
    let speciesRows = '<div class="empty">No sightings recorded yet.</div>';
    if (d.species_ranking && d.species_ranking.length > 0) {
        const maxCount = d.species_ranking[0].count;
        speciesRows = d.species_ranking.map(s => {
            const pct = maxCount > 0 ? Math.max(2, Math.round((s.count / maxCount) * 100)) : 2;
            const thumbHTML = s.photo_url
                ? '<a href="' + s.photo_url + '" target="_blank">'
                  + '<img src="' + s.photo_url + '" class="species-thumb" loading="lazy"'
                  + ' title="' + s.species + ' \u2014 best confidence: '
                  + Math.round((s.best_confidence || 0) * 100) + '%"></a>'
                : '<div class="species-thumb-ph"></div>';
            const inBar  = s.count >= 5 ? '<span class="bar-count">'     + s.count + '</span>' : '';
            const outBar = s.count <  5 ? '<span class="bar-count-out">' + s.count + '</span>' : '';
            return '<div class="species-row">'
                + thumbHTML
                + '<span class="species-name">' + s.species.replace(/_/g, ' ') + '</span>'
                + '<div class="bar-track"><div class="bar-fill" style="width:' + pct + '%">'
                + inBar + '</div></div>' + outBar
                + '</div>';
        }).join('');
    }

    // ── Recent captures ───────────────────────────────────────────────────────
    let captureGrid = '<div class="empty">No captured photos linked yet.</div>';
    if (d.recent_captures && d.recent_captures.length > 0) {
        const cells = d.recent_captures.map(c => {
            const ts   = fmtTs(c.timestamp).slice(5); // MM-DD HH:MM
            const conf = Math.round(c.confidence * 100) + '%';
            return '<a href="' + c.photo_url + '" target="_blank" class="capture-cell">'
                + '<img src="' + c.photo_url + '" loading="lazy" alt="' + c.species + '">'
                + '<div class="capture-label"><strong>'
                + c.species.replace(/_/g, ' ') + '</strong>'
                + ts + '\u2009\u00b7\u2009' + conf
                + '</div></a>';
        }).join('');
        captureGrid = '<div class="capture-grid">' + cells + '</div>';
    }

    // ── Daily chart (last 14 days) ─────────────────────────────────────────
    let dailyChart = '<div class="empty">No data for the last 14 days.</div>';
    if (d.daily_counts && d.daily_counts.length > 0) {
        const maxD    = Math.max(...d.daily_counts.map(x => x.count), 1);
        const peakDay = d.daily_counts.reduce((a, b) => b.count > a.count ? b : a).date;
        dailyChart = '<div class="chart">'
            + d.daily_counts.map(day => {
                const h = Math.max(2, Math.round((day.count / maxD) * 100));
                return '<div class="chart-col">'
                    + '<span class="chart-value">' + day.count + '</span>'
                    + '<div class="chart-bar' + (day.date === peakDay ? ' peak' : '')
                    + '" style="height:' + h + '%" title="' + day.date + ': ' + day.count + ' captures"></div>'
                    + '<span class="chart-label">' + day.date.slice(5) + '</span>'
                    + '</div>';
            }).join('')
            + '</div>';
    }

    // ── Hourly chart ──────────────────────────────────────────────────────────
    let hourlyChart = '<div class="empty">No hourly data yet.</div>';
    if (d.hourly_distribution && Object.keys(d.hourly_distribution).length > 0) {
        const counts  = Array.from({length: 24}, (_, i) => d.hourly_distribution[String(i)] || 0);
        const maxH    = Math.max(...counts, 1);
        const peakH   = counts.indexOf(Math.max(...counts));
        hourlyChart = '<div class="chart">'
            + counts.map((cnt, h) => {
                const barH = cnt > 0 ? Math.max(2, Math.round((cnt / maxH) * 100)) : 0;
                const label = h % 3 === 0 ? String(h).padStart(2, '0') : '';
                return '<div class="chart-col">'
                    + '<span class="chart-value">' + (cnt > 0 ? cnt : '') + '</span>'
                    + '<div class="chart-bar' + (h === peakH && cnt > 0 ? ' peak' : '')
                    + '" style="height:' + barH + '%" title="'
                    + String(h).padStart(2, '0') + ':00 \u2014 ' + cnt + ' sightings"></div>'
                    + '<span class="chart-label">' + label + '</span>'
                    + '</div>';
            }).join('')
            + '</div>';
    }

    document.getElementById('content').innerHTML = `
    <div class="card">
        <h2>Capture Summary</h2>
        <div class="grid-5">
            <div class="metric"><div class="value">${d.total_sightings}</div><div class="label">Total Captures</div></div>
            <div class="metric"><div class="value">${d.today_sightings}</div><div class="label">Today</div></div>
            <div class="metric"><div class="value">${d.unique_species}</div><div class="label">Species</div></div>
            <div class="metric"><div class="value">${d.active_days}</div><div class="label">Active Days</div></div>
            <div class="metric"><div class="value">${d.avg_per_day}</div><div class="label">Avg / Day</div></div>
        </div>
        <div class="meta-row">${metaParts.join(' &nbsp;&middot;&nbsp; ')}</div>
    </div>

    <div class="card">
        <h2>Species Ranking</h2>
        ${speciesRows}
    </div>

    <div class="card">
        <h2>Recent Captures</h2>
        ${captureGrid}
    </div>

    <div class="card">
        <h2>Daily Captures \u2014 Last 14 Days</h2>
        ${dailyChart}
    </div>

    <div class="card">
        <h2>Hourly Activity Distribution</h2>
        ${hourlyChart}
    </div>
    `;
}

function refresh() {
    fetch('/api/stats')
        .then(r => r.json())
        .then(render)
        .catch(e => {
            document.getElementById('content').innerHTML =
                '<div class="card error">Failed to load statistics: ' + e + '</div>';
        });
}

refresh();
setInterval(refresh, 60000);
</script>
</body>
</html>"""


def build_calibration_html() -> str:
    """Build the confidence calibration dashboard HTML page."""
    return CALIBRATION_HTML_TEMPLATE


CALIBRATION_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bird Feeder - Confidence Calibration</title>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           background: #f5f5f5; color: #333; }
    nav { background: #2e7d32; color: white; padding: 1rem 2rem; display: flex;
          justify-content: space-between; align-items: center; }
    nav a { color: white; text-decoration: none; margin-left: 1.5rem; opacity: 0.85; }
    nav a:hover { opacity: 1; }
    nav a.active { opacity: 1; border-bottom: 2px solid white; padding-bottom: 2px; }
    .container { max-width: 1000px; margin: 0 auto; padding: 1.5rem; }
    .card { background: white; border-radius: 8px; padding: 1.5rem;
            margin-bottom: 1rem; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
    .card h2 { color: #2e7d32; margin-bottom: 0.6rem; font-size: 1.1rem; }
    .card-desc { font-size: 0.82rem; color: #777; margin-bottom: 1rem; }
    /* Summary grid */
    .grid-5 { display: grid; grid-template-columns: repeat(5, 1fr); gap: 0.75rem; }
    @media (max-width: 640px) { .grid-5 { grid-template-columns: repeat(3, 1fr); } }
    .metric { text-align: center; padding: 0.85rem 0.5rem; background: #f9f9f9; border-radius: 6px; }
    .metric .value { font-size: 1.75rem; font-weight: bold; color: #2e7d32; }
    .metric .label { font-size: 0.8rem; color: #666; margin-top: 0.25rem; }
    .meta-row { margin-top: 0.85rem; font-size: 0.82rem; color: #777; }
    /* Bar charts (same style as stats page) */
    .chart { display: flex; align-items: flex-end; gap: 3px; height: 110px; padding-top: 0.5rem; }
    .chart-col { display: flex; flex-direction: column; align-items: center;
                 flex: 1; height: 100%; justify-content: flex-end; }
    .chart-bar { width: 100%; background: #81c784; border-radius: 3px 3px 0 0;
                 min-height: 0; transition: height 0.4s ease; }
    .chart-bar.peak { background: #2e7d32; }
    .chart-bar:hover { opacity: 0.8; }
    .chart-label { font-size: 0.62rem; color: #888; margin-top: 3px; text-align: center; }
    .chart-value { font-size: 0.62rem; color: #555; margin-bottom: 1px; min-height: 10px; }
    /* Calibration curve SVG wrapper */
    .curve-wrap { display: flex; justify-content: center; padding: 0.5rem 0; }
    /* Species & inspector tables */
    table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
    th, td { text-align: left; padding: 0.45rem 0.5rem; border-bottom: 1px solid #eee; }
    th { color: #666; font-weight: 600; font-size: 0.82rem; }
    td.conf-cell { font-weight: 600; color: #2e7d32; }
    /* Inspector: thumbnail */
    .insp-thumb { width: 48px; height: 48px; object-fit: cover; border-radius: 4px; display: block; }
    .insp-thumb-ph { width: 48px; height: 48px; background: #e8f5e9; border-radius: 4px; }
    /* Predictions expandable */
    details summary { cursor: pointer; font-size: 0.78rem; color: #555; }
    details summary:hover { color: #2e7d32; }
    ul.preds { list-style: none; padding: 0.3rem 0 0 0.5rem; font-size: 0.78rem; color: #444; }
    ul.preds li { padding: 1px 0; }
    /* Feedback buttons */
    .fb-cell { white-space: nowrap; }
    .btn-correct { background: #e8f5e9; border: 1px solid #a5d6a7; color: #2e7d32;
                   padding: 3px 8px; border-radius: 4px; cursor: pointer; font-size: 0.8rem; }
    .btn-correct:hover { background: #c8e6c9; }
    .btn-wrong { background: #fce4ec; border: 1px solid #f48fb1; color: #c62828;
                 padding: 3px 8px; border-radius: 4px; cursor: pointer; font-size: 0.8rem; }
    .btn-wrong:hover { background: #f8bbd0; }
    .fb-correct { color: #2e7d32; font-size: 0.8rem; font-weight: 600; }
    .fb-wrong { color: #c62828; font-size: 0.8rem; font-weight: 600; }
    .empty { color: #999; font-size: 0.9rem; padding: 0.5rem 0; }
    .refresh-info { text-align: center; color: #999; font-size: 0.8rem; margin-top: 1rem; }
    .error { color: #f44336; }
</style>
</head>
<body>
<nav>
    <strong>Bird Feeder</strong>
    <div>
        <a href="/">Photos</a>
        <a href="/stats">Statistics</a>
        <a href="/calibration" class="active">Calibration</a>
        <a href="/health">Health</a>
    </div>
</nav>
<div class="container">
    <div id="content">Loading calibration data...</div>
    <div class="refresh-info">Auto-refreshes every 30 seconds</div>
</div>
<script>
function fmtConf(v) { return v != null ? Math.round(v * 100) + '%' : '\u2014'; }
function fmtTs(ts) { return ts ? ts.replace('T', ' ').slice(0, 16) : ''; }

function buildCalibrationSVG(bins) {
    const reviewedBins = bins.filter(b => b.reviewed > 0 && b.accuracy != null);
    if (reviewedBins.length === 0) return null;

    const W = 300, H = 220, ML = 44, MB = 34, MR = 15, MT = 14;
    const pw = W - ML - MR, ph = H - MT - MB;
    const xPx = c => ML + c * pw;
    const yPx = a => MT + (1 - a) * ph;

    // Grid lines and diagonal
    let grid = '';
    for (let i = 0; i <= 4; i++) {
        const v = i / 4;
        grid += '<line x1="' + xPx(v).toFixed(1) + '" y1="' + yPx(0).toFixed(1)
            + '" x2="' + xPx(v).toFixed(1) + '" y2="' + yPx(1).toFixed(1)
            + '" stroke="#f0f0f0" stroke-width="1"/>';
        grid += '<line x1="' + xPx(0).toFixed(1) + '" y1="' + yPx(v).toFixed(1)
            + '" x2="' + xPx(1).toFixed(1) + '" y2="' + yPx(v).toFixed(1)
            + '" stroke="#f0f0f0" stroke-width="1"/>';
    }

    // Perfect-calibration diagonal
    const diag = '<line x1="' + xPx(0).toFixed(1) + '" y1="' + yPx(0).toFixed(1)
        + '" x2="' + xPx(1).toFixed(1) + '" y2="' + yPx(1).toFixed(1)
        + '" stroke="#bdbdbd" stroke-width="1.5" stroke-dasharray="5,4"/>';

    // Axes
    const axes = '<line x1="' + xPx(0).toFixed(1) + '" y1="' + yPx(0).toFixed(1)
        + '" x2="' + xPx(1).toFixed(1) + '" y2="' + yPx(0).toFixed(1)
        + '" stroke="#999" stroke-width="1.2"/>'
        + '<line x1="' + xPx(0).toFixed(1) + '" y1="' + yPx(0).toFixed(1)
        + '" x2="' + xPx(0).toFixed(1) + '" y2="' + yPx(1).toFixed(1)
        + '" stroke="#999" stroke-width="1.2"/>';

    // Tick labels
    let ticks = '';
    for (let i = 0; i <= 5; i++) {
        const v = i / 5;
        ticks += '<text x="' + xPx(v).toFixed(1) + '" y="' + (yPx(0) + 11).toFixed(1)
            + '" text-anchor="middle" font-size="8" fill="#888">' + (v * 100).toFixed(0) + '%</text>';
        ticks += '<text x="' + (xPx(0) - 4).toFixed(1) + '" y="' + (yPx(v) + 3).toFixed(1)
            + '" text-anchor="end" font-size="8" fill="#888">' + (v * 100).toFixed(0) + '%</text>';
    }

    // Axis labels
    const axisLabels = '<text x="' + xPx(0.5).toFixed(1) + '" y="' + (H - 2) + '"'
        + ' text-anchor="middle" font-size="9" fill="#666">Model Confidence</text>'
        + '<text x="10" y="' + yPx(0.5).toFixed(1) + '"'
        + ' text-anchor="middle" font-size="9" fill="#666"'
        + ' transform="rotate(-90 10 ' + yPx(0.5).toFixed(1) + ')">Actual Accuracy</text>';

    // Data points (circles sized by sample count)
    const maxRev = Math.max(...reviewedBins.map(b => b.reviewed));
    let points = '';
    reviewedBins.forEach(b => {
        const cx = xPx(b.avg_confidence);
        const cy = yPx(b.accuracy);
        const r = Math.max(4, Math.min(12, 4 + 8 * Math.sqrt(b.reviewed / maxRev)));
        const tip = b.range + ': ' + Math.round(b.accuracy * 100) + '% accurate ('
            + b.correct + '/' + b.reviewed + ' verified)';
        // Shade above/below the diagonal
        const above = b.accuracy > b.avg_confidence;
        points += '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) + '" r="' + r.toFixed(1) + '"'
            + ' fill="' + (above ? '#4caf50' : '#ef9a9a') + '" fill-opacity="0.75"'
            + ' stroke="' + (above ? '#2e7d32' : '#c62828') + '" stroke-width="1.2">'
            + '<title>' + tip + '</title></circle>';
    });

    // Legend
    const legY = MT + 5;
    const legend = '<circle cx="' + (xPx(1) - 6) + '" cy="' + legY + '" r="4" fill="#4caf50" fill-opacity="0.75" stroke="#2e7d32" stroke-width="1"/>'
        + '<text x="' + (xPx(1) - 12) + '" y="' + (legY + 3) + '" text-anchor="end" font-size="7.5" fill="#555">over-confident (green=good)</text>';

    return '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '" style="overflow:visible">'
        + grid + diag + axes + ticks + axisLabels + points + legend + '</svg>';
}

function render(d) {
    if (d.error) {
        document.getElementById('content').innerHTML = '<div class="card error">' + d.error + '</div>';
        return;
    }

    // ── Summary ──────────────────────────────────────────────────────────────────
    const eceText = d.ece != null ? d.ece.toFixed(4) : '\u2014';
    const accText = d.overall_accuracy != null ? Math.round(d.overall_accuracy * 100) + '%' : '\u2014';
    const summaryCard = '<div class="card">'
        + '<h2>Calibration Summary</h2>'
        + '<div class="grid-5">'
        + '<div class="metric"><div class="value">' + d.total_sightings + '</div><div class="label">Total Sightings</div></div>'
        + '<div class="metric"><div class="value">' + fmtConf(d.avg_confidence) + '</div><div class="label">Avg Confidence</div></div>'
        + '<div class="metric"><div class="value">' + d.total_reviewed + '</div><div class="label">Manually Verified</div></div>'
        + '<div class="metric"><div class="value">' + accText + '</div><div class="label">Verified Accuracy</div></div>'
        + '<div class="metric"><div class="value">' + eceText + '</div><div class="label">ECE \u2193</div></div>'
        + '</div>'
        + '<div class="meta-row">Confidence range: <strong>' + fmtConf(d.min_confidence)
        + '</strong> \u2013 <strong>' + fmtConf(d.max_confidence) + '</strong>'
        + ' &nbsp;\u00b7&nbsp; ECE = Expected Calibration Error (lower is better; 0 = perfect)</div>'
        + '</div>';

    // ── Confidence histogram ──────────────────────────────────────────────────────
    let histHtml = '<div class="empty">No sightings recorded yet.</div>';
    if (d.calibration_bins && d.calibration_bins.length > 0) {
        const allBins = [];
        for (let i = 0; i <= 9; i++) {
            allBins.push(d.calibration_bins.find(b => b.bin === i) || {bin: i, total: 0});
        }
        const maxCount = Math.max(...allBins.map(b => b.total), 1);
        histHtml = '<div class="chart">' + allBins.map(bin => {
            const h = bin.total > 0 ? Math.max(2, Math.round((bin.total / maxCount) * 100)) : 0;
            const isPeak = bin.total === maxCount && bin.total > 0;
            return '<div class="chart-col">'
                + '<span class="chart-value">' + (bin.total > 0 ? bin.total : '') + '</span>'
                + '<div class="chart-bar' + (isPeak ? ' peak' : '')
                + '" style="height:' + h + '%" title="' + (bin.bin * 10) + '\u2013' + (bin.bin * 10 + 10) + '%: ' + bin.total + ' sightings"></div>'
                + '<span class="chart-label">' + (bin.bin * 10) + '%</span>'
                + '</div>';
        }).join('') + '</div>';
    }

    // ── Calibration curve ─────────────────────────────────────────────────────────
    let curveHtml = '<div class="empty">Mark sightings as \u2713\u00a0Correct or \u2717\u00a0Wrong below to build this chart. '
        + 'Points on the dashed line = perfectly calibrated model.</div>';
    if (d.calibration_bins) {
        const svg = buildCalibrationSVG(d.calibration_bins);
        if (svg) curveHtml = '<div class="curve-wrap">' + svg + '</div>';
    }

    // ── Daily confidence trend ────────────────────────────────────────────────────
    let dailyHtml = '<div class="empty">No daily data yet.</div>';
    if (d.daily_confidence && d.daily_confidence.length > 0) {
        const maxConf = Math.max(...d.daily_confidence.map(x => x.avg_confidence), 0.01);
        dailyHtml = '<div class="chart">' + d.daily_confidence.map(day => {
            const h = Math.max(2, Math.round((day.avg_confidence / maxConf) * 100));
            const confPct = Math.round(day.avg_confidence * 100) + '%';
            return '<div class="chart-col">'
                + '<span class="chart-value">' + confPct + '</span>'
                + '<div class="chart-bar" style="height:' + h + '%" title="'
                + day.date + ': avg ' + confPct + ' (' + day.count + ' sightings)"></div>'
                + '<span class="chart-label">' + day.date.slice(5) + '</span>'
                + '</div>';
        }).join('') + '</div>';
    }

    // ── Species table ─────────────────────────────────────────────────────────────
    let speciesRows = '<tr><td colspan="6" style="color:#999">No sightings yet.</td></tr>';
    if (d.species_stats && d.species_stats.length > 0) {
        speciesRows = d.species_stats.map(s => {
            const accTxt = s.accuracy != null ? Math.round(s.accuracy * 100) + '%' : '\u2014';
            const verTxt = s.reviewed > 0 ? s.correct + '/' + s.reviewed : '\u2014';
            return '<tr>'
                + '<td>' + s.species.replace(/_/g, ' ') + '</td>'
                + '<td>' + s.count + '</td>'
                + '<td class="conf-cell">' + Math.round(s.avg_confidence * 100) + '%</td>'
                + '<td>' + Math.round(s.min_confidence * 100) + '\u2013' + Math.round(s.max_confidence * 100) + '%</td>'
                + '<td>' + verTxt + '</td>'
                + '<td>' + accTxt + '</td>'
                + '</tr>';
        }).join('');
    }

    // ── Sightings inspector ───────────────────────────────────────────────────────
    let inspRows = '<tr><td colspan="5" style="color:#999">No sightings yet.</td></tr>';
    if (d.recent_sightings && d.recent_sightings.length > 0) {
        inspRows = d.recent_sightings.map(s => {
            const conf = Math.round(s.confidence * 100) + '%';
            const ts = fmtTs(s.timestamp);
            const imgHtml = s.photo_url
                ? '<a href="' + s.photo_url + '" target="_blank"><img src="' + s.photo_url + '" class="insp-thumb" loading="lazy"></a>'
                : '<div class="insp-thumb-ph"></div>';

            let predsHtml = '\u2014';
            if (s.predictions && s.predictions.length > 0) {
                predsHtml = '<details><summary>' + s.predictions.length + ' predictions</summary>'
                    + '<ul class="preds">'
                    + s.predictions.map(p =>
                        '<li>' + p.species.replace(/_/g, ' ') + ': '
                        + Math.round(p.confidence * 100) + '%</li>'
                    ).join('')
                    + '</ul></details>';
            }

            let fbHtml;
            if (s.user_feedback === 1) {
                fbHtml = '<span class="fb-correct">\u2713 Correct</span> '
                    + '<button class="btn-wrong" onclick="setFeedback(' + s.id + ', false, this)">\u2717</button>';
            } else if (s.user_feedback === 0) {
                fbHtml = '<button class="btn-correct" onclick="setFeedback(' + s.id + ', true, this)">\u2713</button> '
                    + '<span class="fb-wrong">\u2717 Wrong</span>';
            } else {
                fbHtml = '<button class="btn-correct" onclick="setFeedback(' + s.id + ', true, this)">\u2713 Correct</button> '
                    + '<button class="btn-wrong" onclick="setFeedback(' + s.id + ', false, this)">\u2717 Wrong</button>';
            }

            return '<tr>'
                + '<td>' + imgHtml + '</td>'
                + '<td><strong>' + s.species.replace(/_/g, ' ') + '</strong><br><small style="color:#888">' + ts + '</small></td>'
                + '<td class="conf-cell">' + conf + '</td>'
                + '<td>' + predsHtml + '</td>'
                + '<td class="fb-cell">' + fbHtml + '</td>'
                + '</tr>';
        }).join('');
    }

    document.getElementById('content').innerHTML = summaryCard + `
    <div class="card">
        <h2>Confidence Distribution</h2>
        <p class="card-desc">Count of sightings per 10% confidence bucket. All stored sightings exceed the acceptance threshold (30%).</p>
        ${histHtml}
    </div>

    <div class="card">
        <h2>Calibration Curve (Reliability Diagram)</h2>
        <p class="card-desc">For each confidence bucket: what fraction of manually verified sightings were actually correct?
        Green circles = model is underconfident (good). Red = overconfident. Circle size = number of verified samples.</p>
        ${curveHtml}
    </div>

    <div class="card">
        <h2>Daily Average Confidence \u2014 Last 30 Days</h2>
        <p class="card-desc">Track whether model confidence is trending up or down over time.</p>
        ${dailyHtml}
    </div>

    <div class="card">
        <h2>Confidence by Species</h2>
        <table>
            <tr><th>Species</th><th>Sightings</th><th>Avg Conf</th><th>Range</th><th>Verified</th><th>Accuracy</th></tr>
            ${speciesRows}
        </table>
    </div>

    <div class="card">
        <h2>Sightings Inspector</h2>
        <p class="card-desc">Last 50 classifications. Mark each as correct or incorrect to build the calibration curve.
        Click "N predictions" to inspect the full top-K model output.</p>
        <table class="insp-table">
            <tr><th>Photo</th><th>Species &amp; Time</th><th>Conf</th><th>Predictions</th><th>Feedback</th></tr>
            ${inspRows}
        </table>
    </div>
    `;
}

function setFeedback(id, correct, btn) {
    const cell = btn.closest('td');
    cell.innerHTML = '<span style="color:#999;font-size:0.8rem">Saving\u2026</span>';
    fetch('/api/feedback', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: id, correct: correct})
    })
    .then(r => r.json())
    .then(data => {
        if (data.ok) {
            if (correct) {
                cell.innerHTML = '<span class="fb-correct">\u2713 Correct</span> '
                    + '<button class="btn-wrong" onclick="setFeedback(' + id + ', false, this)">\u2717</button>';
            } else {
                cell.innerHTML = '<button class="btn-correct" onclick="setFeedback(' + id + ', true, this)">\u2713</button> '
                    + '<span class="fb-wrong">\u2717 Wrong</span>';
            }
        } else {
            cell.innerHTML = '<span class="error">Error saving</span>';
        }
    })
    .catch(() => { cell.innerHTML = '<span class="error">Network error</span>'; });
}

function refresh() {
    fetch('/api/calibration')
        .then(r => r.json())
        .then(render)
        .catch(e => {
            document.getElementById('content').innerHTML =
                '<div class="card error">Failed to load calibration data: ' + e + '</div>';
        });
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>"""


def build_health_html() -> str:
    """Build the health dashboard HTML page."""
    return HEALTH_HTML_TEMPLATE


# The health HTML is a self-contained page that fetches /api/health via JS
HEALTH_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bird Feeder - System Health</title>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           background: #f5f5f5; color: #333; }
    nav { background: #2e7d32; color: white; padding: 1rem 2rem; display: flex;
          justify-content: space-between; align-items: center; }
    nav a { color: white; text-decoration: none; margin-left: 1.5rem; opacity: 0.85; }
    nav a:hover { opacity: 1; }
    nav a.active { opacity: 1; border-bottom: 2px solid white; padding-bottom: 2px; }
    .container { max-width: 900px; margin: 0 auto; padding: 1.5rem; }
    .card { background: white; border-radius: 8px; padding: 1.5rem;
            margin-bottom: 1rem; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
    .card h2 { color: #2e7d32; margin-bottom: 1rem; font-size: 1.1rem; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; }
    .metric { text-align: center; padding: 1rem; background: #f9f9f9; border-radius: 6px; }
    .metric .value { font-size: 2rem; font-weight: bold; color: #2e7d32; }
    .metric .label { font-size: 0.85rem; color: #666; margin-top: 0.3rem; }
    .bar-bg { background: #e0e0e0; border-radius: 4px; height: 20px; margin-top: 0.5rem; }
    .bar-fill { height: 100%; border-radius: 4px; transition: width 0.5s; }
    .status-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
                  margin-right: 6px; vertical-align: middle; }
    .status-running { background: #4caf50; }
    .status-stopped { background: #f44336; }
    .status-unknown { background: #ff9800; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid #eee; }
    th { color: #666; font-weight: 600; font-size: 0.85rem; }
    .refresh-info { text-align: center; color: #999; font-size: 0.8rem; margin-top: 1rem; }
    .error { color: #f44336; }
    .ok { color: #4caf50; }
    .warn { color: #ff9800; }
</style>
</head>
<body>
<nav>
    <strong>Bird Feeder</strong>
    <div>
        <a href="/">Photos</a>
        <a href="/stats">Statistics</a>
        <a href="/calibration">Calibration</a>
        <a href="/health" class="active">Health</a>
    </div>
</nav>
<div class="container">
    <div id="dashboard">Loading...</div>
    <div class="refresh-info">Auto-refreshes every 30 seconds</div>
</div>
<script>
function diskColor(pct) {
    if (pct > 90) return '#f44336';
    if (pct > 75) return '#ff9800';
    return '#4caf50';
}

function render(d) {
    const disk = d.disk;
    const photos = d.photos;
    const db = d.database;
    const proc = d.processes;

    let speciesRows = '';
    if (photos.species && photos.species.length > 0) {
        photos.species.forEach(s => {
            speciesRows += '<tr><td>' + s.name.replace(/_/g, ' ') + '</td><td>' + s.count + '</td></tr>';
        });
    } else {
        speciesRows = '<tr><td colspan="2" style="color:#999">No species detected yet</td></tr>';
    }

    let recentRows = '';
    if (db.recent && db.recent.length > 0) {
        db.recent.forEach(r => {
            const ts = r.timestamp.replace('T', ' ').substring(0, 19);
            const conf = (r.confidence * 100).toFixed(1) + '%';
            recentRows += '<tr><td>' + ts + '</td><td>' + r.species + '</td><td>' + conf + '</td></tr>';
        });
    } else {
        recentRows = '<tr><td colspan="3" style="color:#999">No sightings recorded yet</td></tr>';
    }

    const captureStatus = proc.motion_detector || 'unknown';
    const classifyStatus = proc.classifier || 'unknown';

    document.getElementById('dashboard').innerHTML = `
    <div class="card">
        <h2>Processes</h2>
        <div class="grid">
            <div class="metric">
                <span class="status-dot status-${captureStatus}"></span>
                Motion Detector: <strong>${captureStatus}</strong>
            </div>
            <div class="metric">
                <span class="status-dot status-${classifyStatus}"></span>
                Classifier: <strong>${classifyStatus}</strong>
            </div>
        </div>
    </div>

    <div class="card">
        <h2>Disk Usage</h2>
        <div class="grid">
            <div class="metric"><div class="value">${disk.free_gb} GB</div><div class="label">Free</div></div>
            <div class="metric"><div class="value">${disk.used_gb} GB</div><div class="label">Used</div></div>
            <div class="metric"><div class="value">${disk.total_gb} GB</div><div class="label">Total</div></div>
        </div>
        <div class="bar-bg">
            <div class="bar-fill" style="width:${disk.used_pct}%;background:${diskColor(disk.used_pct)}"></div>
        </div>
        <div style="text-align:center;margin-top:4px;font-size:0.85rem;color:#666">${disk.used_pct}% used</div>
    </div>

    <div class="card">
        <h2>Photos</h2>
        <div class="grid">
            <div class="metric"><div class="value">${photos.classified}</div><div class="label">Classified</div></div>
            <div class="metric"><div class="value">${photos.in_queue}</div><div class="label">In Queue</div></div>
            <div class="metric"><div class="value">${photos.unclassified}</div><div class="label">Unclassified</div></div>
        </div>
    </div>

    <div class="card">
        <h2>Species Breakdown</h2>
        <table>
            <tr><th>Species</th><th>Photos</th></tr>
            ${speciesRows}
        </table>
    </div>

    <div class="card">
        <h2>Database</h2>
        <div class="grid">
            <div class="metric"><div class="value">${db.total_sightings || 0}</div><div class="label">Total Sightings</div></div>
            <div class="metric"><div class="value">${db.unique_species || 0}</div><div class="label">Unique Species</div></div>
            <div class="metric"><div class="value">${db.today_sightings || 0}</div><div class="label">Today</div></div>
            <div class="metric"><div class="value">${d.database_size_mb} MB</div><div class="label">DB Size</div></div>
        </div>
    </div>

    <div class="card">
        <h2>Recent Sightings</h2>
        <table>
            <tr><th>Time</th><th>Species</th><th>Confidence</th></tr>
            ${recentRows}
        </table>
    </div>

    <div class="card">
        <h2>Logs</h2>
        <div class="metric"><div class="value">${d.log_size_mb} MB</div><div class="label">Log File Size</div></div>
    </div>

    ${renderPowerCard(d.power)}
    `;
}

function tempColor(c) {
    if (c === null || c === undefined) return '#666';
    if (c > 80) return '#f44336';
    if (c > 70) return '#ff9800';
    return '#4caf50';
}

function renderPowerCard(p) {
    if (!p) return '';
    const cur = p.current || {};
    const hist = p.history || [];

    const tempVal = cur.cpu_temp_c !== null && cur.cpu_temp_c !== undefined
        ? cur.cpu_temp_c.toFixed(1) + '\u00b0C' : '--';
    const voltsVal = cur.core_volts_v !== null && cur.core_volts_v !== undefined
        ? cur.core_volts_v.toFixed(4) + ' V' : '--';

    const isThrottled = cur.throttled && cur.throttled !== '0x0';
    const throttleText = cur.throttled !== null && cur.throttled !== undefined
        ? (isThrottled ? 'THROTTLED' : 'OK') : '--';
    const throttleDetail = isThrottled ? ' (' + cur.throttled + ')' : '';
    const throttleColor = isThrottled ? '#f44336' : '#4caf50';

    let histHtml = '';
    if (hist.length > 0) {
        const rows = [...hist].reverse().slice(0, 12).map(r => {
            const tc = r.cpu_temp_c !== null && r.cpu_temp_c !== undefined
                ? '<span style="color:' + tempColor(r.cpu_temp_c) + '">' + r.cpu_temp_c.toFixed(1) + '\u00b0C</span>'
                : '--';
            const vc = r.core_volts_v !== null && r.core_volts_v !== undefined
                ? r.core_volts_v.toFixed(4) + ' V' : '--';
            return '<tr><td>' + r.ts + '</td><td>' + tc + '</td><td>' + vc + '</td></tr>';
        }).join('');
        histHtml = '<details style="margin-top:1rem"><summary style="cursor:pointer;color:#666;font-size:0.9rem">Recent history (' + hist.length + ' readings)</summary>'
            + '<table style="margin-top:0.5rem"><tr><th>Time</th><th>Temp</th><th>Voltage</th></tr>'
            + rows + '</table></details>';
    }

    return '<div class="card"><h2>Power &amp; Thermal</h2>'
        + '<div class="grid">'
        + '<div class="metric"><div class="value" style="color:' + tempColor(cur.cpu_temp_c) + '">' + tempVal + '</div><div class="label">CPU Temperature</div></div>'
        + '<div class="metric"><div class="value">' + voltsVal + '</div><div class="label">Core Voltage</div></div>'
        + '<div class="metric"><div class="value" style="color:' + throttleColor + '">' + throttleText + '<span style="font-size:0.65em">' + throttleDetail + '</span></div><div class="label">Throttle Status</div></div>'
        + '</div>' + histHtml + '</div>';
}

function refresh() {
    fetch('/api/health')
        .then(r => r.json())
        .then(render)
        .catch(e => {
            document.getElementById('dashboard').innerHTML =
                '<div class="card error">Failed to load health data: ' + e + '</div>';
        });
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>"""


class BirdFeederHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the bird feeder web interface."""

    def do_GET(self):
        path = unquote(self.path).rstrip("/") or "/"

        if path == "/":
            self._serve_html(build_gallery_html())
        elif path == "/health":
            self._serve_html(build_health_html())
        elif path == "/stats":
            self._serve_html(build_stats_html())
        elif path == "/calibration":
            self._serve_html(build_calibration_html())
        elif path == "/api/health":
            self._serve_json(get_health_data())
        elif path == "/api/stats":
            self._serve_stats_json()
        elif path == "/api/calibration":
            self._serve_calibration_json()
        elif path.startswith("/photos/"):
            self._serve_photo(path[len("/photos/") :])
        else:
            self._send_error(404, "Not found")

    def do_POST(self):
        path = unquote(self.path).rstrip("/") or "/"
        if path == "/api/feedback":
            self._handle_feedback()
        else:
            self._send_error(404, "Not found")

    def _serve_html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self, data: dict):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_stats_json(self):
        """Serve live capture statistics from the database."""
        if not DB_PATH.exists():
            self._serve_json({"error": "No database found yet"})
            return
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row

            total = conn.execute("SELECT COUNT(*) as c FROM sightings").fetchone()["c"]
            today = datetime.now().strftime("%Y-%m-%d")
            today_count = conn.execute(
                "SELECT COUNT(*) as c FROM sightings WHERE date = ?", (today,)
            ).fetchone()["c"]
            unique = conn.execute("SELECT COUNT(DISTINCT species) as c FROM sightings").fetchone()[
                "c"
            ]
            first = conn.execute("SELECT MIN(date) as d FROM sightings").fetchone()["d"]
            active_days = conn.execute(
                "SELECT COUNT(DISTINCT date) as d FROM sightings"
            ).fetchone()["d"]

            # Species ranking with best-confidence photo per species
            species_rows = conn.execute(
                """SELECT s.species, COUNT(*) as count,
                          bp.image_path as best_photo,
                          bp.confidence as best_confidence
                   FROM sightings s
                   LEFT JOIN (
                       SELECT species, image_path, MAX(confidence) as confidence
                       FROM sightings WHERE image_path IS NOT NULL
                       GROUP BY species
                   ) bp ON s.species = bp.species
                   GROUP BY s.species ORDER BY count DESC"""
            ).fetchall()

            hourly = conn.execute(
                "SELECT hour, COUNT(*) as count FROM sightings GROUP BY hour ORDER BY hour"
            ).fetchall()
            daily = conn.execute(
                "SELECT date, COUNT(*) as count FROM sightings "
                "WHERE date >= date('now', '-14 days') "
                "GROUP BY date ORDER BY date"
            ).fetchall()

            # Most captures in a single day
            most_active = conn.execute(
                "SELECT date, COUNT(*) as count FROM sightings "
                "GROUP BY date ORDER BY count DESC LIMIT 1"
            ).fetchone()

            # 16 most recent captures that have a photo
            recent = conn.execute(
                "SELECT timestamp, species, confidence, image_path "
                "FROM sightings WHERE image_path IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT 16"
            ).fetchall()

            conn.close()

            avg_per_day = round(total / active_days, 1) if active_days > 0 else 0
            peak_hour = max(hourly, key=lambda r: r["count"])["hour"] if hourly else None

            self._serve_json(
                {
                    "total_sightings": total,
                    "today_sightings": today_count,
                    "unique_species": unique,
                    "first_sighting": first,
                    "active_days": active_days,
                    "avg_per_day": avg_per_day,
                    "peak_hour": peak_hour,
                    "most_active_day": (
                        {"date": most_active["date"], "count": most_active["count"]}
                        if most_active
                        else None
                    ),
                    "species_ranking": [
                        {
                            "species": r["species"],
                            "count": r["count"],
                            "photo_url": _image_path_to_url(r["best_photo"]),
                            "best_confidence": (
                                round(r["best_confidence"], 3)
                                if r["best_confidence"] is not None
                                else None
                            ),
                        }
                        for r in species_rows
                    ],
                    "hourly_distribution": {str(r["hour"]): r["count"] for r in hourly},
                    "daily_counts": [{"date": r["date"], "count": r["count"]} for r in daily],
                    "recent_captures": [
                        {
                            "timestamp": r["timestamp"],
                            "species": r["species"],
                            "confidence": round(r["confidence"], 3),
                            "photo_url": _image_path_to_url(r["image_path"]),
                        }
                        for r in recent
                        if _image_path_to_url(r["image_path"])
                    ],
                    "generated_at": datetime.now().isoformat(),
                }
            )
        except Exception as e:
            self._serve_json({"error": f"Database error: {e}"})

    def _handle_feedback(self):
        """Handle POST /api/feedback — store user verification of a sighting."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body)
            sighting_id = int(data["id"])
            is_correct = bool(data["correct"])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
            self._send_error(400, f"Bad request: {e}")
            return

        if not DB_PATH.exists():
            self._send_error(500, "Database not found")
            return

        try:
            conn = sqlite3.connect(str(DB_PATH))
            _ensure_feedback_column(conn)
            cursor = conn.execute(
                "UPDATE sightings SET user_feedback = ? WHERE id = ?",
                (1 if is_correct else 0, sighting_id),
            )
            conn.commit()
            rows_changed = cursor.rowcount
            conn.close()
            if rows_changed == 0:
                self._send_error(404, f"Sighting {sighting_id} not found")
            else:
                self._serve_json({"ok": True, "id": sighting_id, "correct": is_correct})
        except Exception as e:
            self._send_error(500, f"Database error: {e}")

    def _serve_calibration_json(self):
        """Serve confidence calibration metrics and debugging data."""
        if not DB_PATH.exists():
            self._serve_json({"error": "No database found yet"})
            return
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            _ensure_feedback_column(conn)

            # Overall confidence stats and feedback summary
            overall = conn.execute("""
                SELECT COUNT(*) as total,
                       AVG(confidence) as avg_conf,
                       MIN(confidence) as min_conf,
                       MAX(confidence) as max_conf,
                       SUM(CASE WHEN user_feedback IS NOT NULL THEN 1 ELSE 0 END) as reviewed,
                       SUM(CASE WHEN user_feedback = 1 THEN 1 ELSE 0 END) as correct
                FROM sightings
            """).fetchone()

            # Per-bin stats: confidence grouped into 10% buckets (0–9, capped so 1.0 → bin 9)
            bin_rows = conn.execute("""
                SELECT MIN(CAST(confidence * 10 AS INTEGER), 9) as bin,
                       COUNT(*) as total,
                       SUM(CASE WHEN user_feedback IS NOT NULL THEN 1 ELSE 0 END) as reviewed,
                       SUM(CASE WHEN user_feedback = 1 THEN 1 ELSE 0 END) as correct,
                       AVG(confidence) as avg_conf
                FROM sightings
                GROUP BY MIN(CAST(confidence * 10 AS INTEGER), 9)
                ORDER BY bin
            """).fetchall()

            calibration_bins = []
            for row in bin_rows:
                b = row["bin"]
                rev = row["reviewed"] or 0
                cor = int(row["correct"] or 0)
                calibration_bins.append(
                    {
                        "bin": b,
                        "range": f"{b * 10}–{b * 10 + 10}%",
                        "total": row["total"],
                        "reviewed": rev,
                        "correct": cor,
                        "avg_confidence": round(row["avg_conf"], 3),
                        "accuracy": round(cor / rev, 3) if rev > 0 else None,
                    }
                )

            # Species-level confidence breakdown
            species_rows = conn.execute("""
                SELECT species,
                       COUNT(*) as count,
                       AVG(confidence) as avg_conf,
                       MIN(confidence) as min_conf,
                       MAX(confidence) as max_conf,
                       SUM(CASE WHEN user_feedback IS NOT NULL THEN 1 ELSE 0 END) as reviewed,
                       SUM(CASE WHEN user_feedback = 1 THEN 1 ELSE 0 END) as correct
                FROM sightings
                GROUP BY species ORDER BY count DESC
            """).fetchall()

            # Daily average confidence for the last 30 days
            daily_rows = conn.execute("""
                SELECT date, AVG(confidence) as avg_conf, COUNT(*) as count
                FROM sightings
                WHERE date >= date('now', '-30 days')
                GROUP BY date ORDER BY date
            """).fetchall()

            # 50 most recent sightings for the inspector
            recent_rows = conn.execute("""
                SELECT id, timestamp, species, confidence,
                       image_path, predictions_json, user_feedback
                FROM sightings ORDER BY timestamp DESC LIMIT 50
            """).fetchall()

            conn.close()

            reviewed = overall["reviewed"] or 0
            correct = int(overall["correct"] or 0)
            overall_accuracy = round(correct / reviewed, 3) if reviewed > 0 else None

            # Expected Calibration Error (ECE): weighted mean |accuracy - confidence| per bin
            ece = None
            total_reviewed_bins = sum(b["reviewed"] for b in calibration_bins)
            if total_reviewed_bins > 0:
                ece = round(
                    sum(
                        (b["reviewed"] / total_reviewed_bins)
                        * abs((b["accuracy"] or 0) - b["avg_confidence"])
                        for b in calibration_bins
                        if b["accuracy"] is not None
                    ),
                    4,
                )

            self._serve_json(
                {
                    "total_sightings": overall["total"] or 0,
                    "avg_confidence": round(overall["avg_conf"], 3)
                    if overall["avg_conf"]
                    else None,
                    "min_confidence": round(overall["min_conf"], 3)
                    if overall["min_conf"]
                    else None,
                    "max_confidence": round(overall["max_conf"], 3)
                    if overall["max_conf"]
                    else None,
                    "total_reviewed": reviewed,
                    "total_correct": correct,
                    "overall_accuracy": overall_accuracy,
                    "ece": ece,
                    "calibration_bins": calibration_bins,
                    "species_stats": [
                        {
                            "species": r["species"],
                            "count": r["count"],
                            "avg_confidence": round(r["avg_conf"], 3),
                            "min_confidence": round(r["min_conf"], 3),
                            "max_confidence": round(r["max_conf"], 3),
                            "reviewed": r["reviewed"] or 0,
                            "correct": int(r["correct"] or 0),
                            "accuracy": (
                                round(int(r["correct"] or 0) / (r["reviewed"]), 3)
                                if (r["reviewed"] or 0) > 0
                                else None
                            ),
                        }
                        for r in species_rows
                    ],
                    "daily_confidence": [
                        {
                            "date": r["date"],
                            "avg_confidence": round(r["avg_conf"], 3),
                            "count": r["count"],
                        }
                        for r in daily_rows
                    ],
                    "recent_sightings": [
                        {
                            "id": r["id"],
                            "timestamp": r["timestamp"],
                            "species": r["species"],
                            "confidence": round(r["confidence"], 3),
                            "photo_url": _image_path_to_url(r["image_path"]),
                            "predictions": (
                                json.loads(r["predictions_json"]) if r["predictions_json"] else None
                            ),
                            "user_feedback": r["user_feedback"],
                        }
                        for r in recent_rows
                    ],
                    "generated_at": datetime.now().isoformat(),
                }
            )
        except Exception as e:
            self._serve_json({"error": f"Database error: {e}"})

    def _serve_photo(self, rel_path: str):
        photo_path = CLASSIFIED_DIR / rel_path

        # Prevent path traversal
        try:
            photo_path = photo_path.resolve()
            if not str(photo_path).startswith(str(CLASSIFIED_DIR.resolve())):
                self._send_error(403, "Forbidden")
                return
        except (ValueError, OSError):
            self._send_error(400, "Bad request")
            return

        if not photo_path.is_file():
            self._send_error(404, "Photo not found")
            return

        content_type = mimetypes.guess_type(str(photo_path))[0] or "image/jpeg"
        try:
            data = photo_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self._send_error(500, "Error reading file")

    def _send_error(self, code: int, message: str):
        body = f"<h1>{code}</h1><p>{message}</p>".encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        if LOGGER:
            LOGGER.debug(f"HTTP {args[0]}")


def main():
    global CONFIG, LOGGER, DATA_DIR, CLASSIFIED_DIR, STATS_DIR, DB_PATH, POWER_CFG

    parser = argparse.ArgumentParser(description="Bird feeder photo browser")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", type=str, default=None)
    args = parser.parse_args()

    CONFIG = load_config(args.config)
    LOGGER = setup_logging(CONFIG)
    storage = CONFIG["storage"]
    web_cfg = CONFIG.get("webserver", {})

    DATA_DIR = PROJECT_ROOT / storage.get("data_dir", "data")
    CLASSIFIED_DIR = PROJECT_ROOT / storage.get("classified_dir", "data/classified")
    STATS_DIR = PROJECT_ROOT / storage.get("stats_dir", "data/stats")
    DB_PATH = PROJECT_ROOT / storage.get("database_path", "data/birds.db")

    POWER_CFG = CONFIG.get("power_monitoring", {})
    if POWER_CFG.get("enabled", False):
        t = threading.Thread(target=_power_log_worker, daemon=True, name="power-monitor")
        t.start()
        LOGGER.info(
            f"Power monitoring started (interval: {POWER_CFG.get('interval_seconds', 300)}s, "
            f"log: {POWER_CFG.get('log_file', 'data/power_log.csv')})"
        )

    host = args.host or web_cfg.get("host", "0.0.0.0")  # nosec B104 – intentional LAN binding
    port = args.port or web_cfg.get("port", 8080)

    class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True  # threads exit when the main process does

    server = ThreadedHTTPServer((host, port), BirdFeederHandler)
    LOGGER.info(f"Web server starting on http://{host}:{port}")
    print(f"Bird Feeder photo browser running at http://{host}:{port}")
    print(f"  Gallery:      http://{host}:{port}/")
    print(f"  Statistics:   http://{host}:{port}/stats")
    print(f"  Calibration:  http://{host}:{port}/calibration")
    print(f"  Health:       http://{host}:{port}/health")
    print(f"  Stats API:    http://{host}:{port}/api/stats")
    print(f"  Calib API:    http://{host}:{port}/api/calibration")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
