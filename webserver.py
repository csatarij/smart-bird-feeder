#!/usr/bin/env python3
"""Local network web server for browsing bird photos and system health.

Runs on the Pi and serves:
  /              — Photo gallery (browse by species, date)
  /health        — System health dashboard
  /api/stats     — JSON stats endpoint
  /api/health    — JSON health data
  /photos/<path> — Classified bird photos

Uses only the Python standard library + project dependencies (no Flask needed).

Usage:
    python3 webserver.py [--config settings.yaml] [--port 8080]
"""

import argparse
import json
import mimetypes
import shutil
import sqlite3
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote

from utils import load_config, setup_logging, PROJECT_ROOT

# Resolved once at startup
CONFIG = None
LOGGER = None
DATA_DIR = None
CLASSIFIED_DIR = None
STATS_DIR = None
DB_PATH = None


def get_health_data() -> dict:
    """Gather system health metrics."""
    disk = shutil.disk_usage(str(DATA_DIR))
    disk_total_gb = disk.total / (1024 ** 3)
    disk_used_gb = disk.used / (1024 ** 3)
    disk_free_gb = disk.free / (1024 ** 3)
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
        len(list(unclassified_dir.glob("*.jpg")))
        if unclassified_dir.exists()
        else 0
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
            unique = conn.execute(
                "SELECT COUNT(DISTINCT species) as c FROM sightings"
            ).fetchone()["c"]
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
                species_list.append({
                    "name": d.name.replace("_", " ").title(),
                    "dir": d.name,
                    "count": len(photos),
                    "recent": [p.name for p in photos[:12]],
                })

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
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 1rem; }
    .metric { text-align: center; padding: 1rem; background: #f9f9f9; border-radius: 6px; }
    .metric .value { font-size: 2rem; font-weight: bold; color: #2e7d32; }
    .metric .label { font-size: 0.85rem; color: #666; margin-top: 0.3rem; }
    /* Species bars */
    .species-row { display: flex; align-items: center; margin-bottom: 0.6rem; gap: 0.75rem; }
    .species-name { min-width: 130px; font-size: 0.9rem; text-transform: capitalize; }
    .bar-track { flex: 1; background: #e8f5e9; border-radius: 4px; height: 22px; overflow: hidden; }
    .bar-fill { height: 100%; background: #4caf50; border-radius: 4px;
                transition: width 0.4s ease; display: flex; align-items: center;
                padding-left: 6px; min-width: 2px; }
    .bar-count { font-size: 0.8rem; color: white; font-weight: 600; white-space: nowrap; }
    .bar-count-outside { font-size: 0.8rem; color: #555; margin-left: 6px; }
    /* Daily / hourly charts */
    .chart { display: flex; align-items: flex-end; gap: 4px; height: 120px;
             padding-top: 0.5rem; }
    .chart-col { display: flex; flex-direction: column; align-items: center;
                 flex: 1; height: 100%; justify-content: flex-end; }
    .chart-bar { width: 100%; background: #81c784; border-radius: 3px 3px 0 0;
                 min-height: 2px; transition: height 0.4s ease; }
    .chart-bar:hover { background: #4caf50; }
    .chart-label { font-size: 0.65rem; color: #888; margin-top: 3px; text-align: center; }
    .chart-value { font-size: 0.65rem; color: #555; margin-bottom: 2px; }
    .empty { color: #999; font-size: 0.9rem; padding: 1rem 0; }
    .refresh-info { text-align: center; color: #999; font-size: 0.8rem; margin-top: 1rem; }
    .error { color: #f44336; }
    .first-seen { font-size: 0.8rem; color: #888; margin-top: 0.5rem; }
</style>
</head>
<body>
<nav>
    <strong>Bird Feeder</strong>
    <div>
        <a href="/">Photos</a>
        <a href="/stats" class="active">Statistics</a>
        <a href="/health">Health</a>
    </div>
</nav>
<div class="container">
    <div id="content">Loading statistics...</div>
    <div class="refresh-info">Auto-refreshes every 60 seconds</div>
</div>
<script>
function render(d) {
    if (d.error) {
        document.getElementById('content').innerHTML =
            '<div class="card error">' + d.error + '</div>';
        return;
    }

    // --- Summary cards ---
    const firstSeen = d.first_sighting
        ? '<div class="first-seen">First sighting: ' + d.first_sighting + '</div>'
        : '';

    // --- Species ranking bars ---
    const total = d.total_sightings || 1;
    let speciesBars = '';
    if (d.species_ranking && d.species_ranking.length > 0) {
        const maxCount = d.species_ranking[0].count;
        d.species_ranking.forEach(s => {
            const pct = maxCount > 0 ? Math.max(2, Math.round((s.count / maxCount) * 100)) : 2;
            const label = s.count >= 4
                ? '<span class="bar-count">' + s.count + '</span>'
                : '';
            const outside = s.count < 4
                ? '<span class="bar-count-outside">' + s.count + '</span>'
                : '';
            speciesBars += '<div class="species-row">'
                + '<span class="species-name">' + s.species.replace(/_/g, ' ') + '</span>'
                + '<div class="bar-track"><div class="bar-fill" style="width:' + pct + '%">'
                + label + '</div></div>' + outside
                + '</div>';
        });
    } else {
        speciesBars = '<div class="empty">No sightings recorded yet.</div>';
    }

    // --- Daily chart (last 14 days) ---
    let dailyChart = '';
    if (d.daily_counts && d.daily_counts.length > 0) {
        const maxDaily = Math.max(...d.daily_counts.map(x => x.count), 1);
        dailyChart = '<div class="chart">';
        d.daily_counts.forEach(day => {
            const h = Math.max(2, Math.round((day.count / maxDaily) * 100));
            const shortDate = day.date.slice(5); // MM-DD
            dailyChart += '<div class="chart-col">'
                + '<span class="chart-value">' + day.count + '</span>'
                + '<div class="chart-bar" style="height:' + h + '%" title="' + day.date + ': ' + day.count + '"></div>'
                + '<span class="chart-label">' + shortDate + '</span>'
                + '</div>';
        });
        dailyChart += '</div>';
    } else {
        dailyChart = '<div class="empty">No data for the last 14 days.</div>';
    }

    // --- Hourly distribution chart ---
    let hourlyChart = '';
    if (d.hourly_distribution && Object.keys(d.hourly_distribution).length > 0) {
        const hours = Array.from({length: 24}, (_, i) => i);
        const counts = hours.map(h => d.hourly_distribution[String(h)] || 0);
        const maxHourly = Math.max(...counts, 1);
        hourlyChart = '<div class="chart">';
        hours.forEach((h, i) => {
            const barH = Math.max(counts[i] > 0 ? 2 : 0, Math.round((counts[i] / maxHourly) * 100));
            const label = h % 3 === 0 ? String(h).padStart(2, '0') : '';
            hourlyChart += '<div class="chart-col">'
                + (counts[i] > 0 ? '<span class="chart-value">' + counts[i] + '</span>' : '<span class="chart-value">&nbsp;</span>')
                + '<div class="chart-bar" style="height:' + barH + '%" title="' + h + ':00 — ' + counts[i] + ' sightings"></div>'
                + '<span class="chart-label">' + label + '</span>'
                + '</div>';
        });
        hourlyChart += '</div>';
    } else {
        hourlyChart = '<div class="empty">No hourly data yet.</div>';
    }

    document.getElementById('content').innerHTML = `
    <div class="card">
        <h2>Capture Summary</h2>
        <div class="grid">
            <div class="metric"><div class="value">${d.total_sightings}</div><div class="label">Total Captures</div></div>
            <div class="metric"><div class="value">${d.today_sightings}</div><div class="label">Today</div></div>
            <div class="metric"><div class="value">${d.unique_species}</div><div class="label">Species</div></div>
            <div class="metric"><div class="value">${d.active_days}</div><div class="label">Active Days</div></div>
        </div>
        ${firstSeen}
    </div>

    <div class="card">
        <h2>Species Ranking</h2>
        ${speciesBars}
    </div>

    <div class="card">
        <h2>Daily Captures — Last 14 Days</h2>
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
    `;
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
        elif path == "/api/health":
            self._serve_json(get_health_data())
        elif path == "/api/stats":
            self._serve_stats_json()
        elif path.startswith("/photos/"):
            self._serve_photo(path[len("/photos/"):])
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
            unique = conn.execute(
                "SELECT COUNT(DISTINCT species) as c FROM sightings"
            ).fetchone()["c"]
            first = conn.execute("SELECT MIN(date) as d FROM sightings").fetchone()["d"]
            active_days = conn.execute(
                "SELECT COUNT(DISTINCT date) as d FROM sightings"
            ).fetchone()["d"]

            species_counts = conn.execute(
                "SELECT species, COUNT(*) as count FROM sightings "
                "GROUP BY species ORDER BY count DESC"
            ).fetchall()
            hourly = conn.execute(
                "SELECT hour, COUNT(*) as count FROM sightings "
                "GROUP BY hour ORDER BY hour"
            ).fetchall()
            daily = conn.execute(
                "SELECT date, COUNT(*) as count FROM sightings "
                "WHERE date >= date('now', '-14 days') "
                "GROUP BY date ORDER BY date"
            ).fetchall()

            conn.close()
            self._serve_json({
                "total_sightings": total,
                "today_sightings": today_count,
                "unique_species": unique,
                "first_sighting": first,
                "active_days": active_days,
                "species_ranking": [
                    {"species": r["species"], "count": r["count"]}
                    for r in species_counts
                ],
                "hourly_distribution": {
                    str(r["hour"]): r["count"] for r in hourly
                },
                "daily_counts": [
                    {"date": r["date"], "count": r["count"]} for r in daily
                ],
                "generated_at": datetime.now().isoformat(),
            })
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
    global CONFIG, LOGGER, DATA_DIR, CLASSIFIED_DIR, STATS_DIR, DB_PATH

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

    host = args.host or web_cfg.get("host", "0.0.0.0")
    port = args.port or web_cfg.get("port", 8080)

    server = HTTPServer((host, port), BirdFeederHandler)
    LOGGER.info(f"Web server starting on http://{host}:{port}")
    print(f"Bird Feeder photo browser running at http://{host}:{port}")
    print(f"  Gallery:   http://{host}:{port}/")
    print(f"  Health:    http://{host}:{port}/health")
    print(f"  Stats API: http://{host}:{port}/api/stats")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
