#!/usr/bin/env python3
"""Generate a static health.html file with current system status.

Run this manually or via cron to produce a self-contained HTML file you
can open in any browser (no web server needed):

    python3 generate_health_html.py [--config settings.yaml]
    # Then open data/health.html in a browser

The web server at /health is preferred for live data, but this script is
useful when you want to copy the file off the Pi for inspection.
"""

import argparse
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from utils import PROJECT_ROOT, load_config


def main():
    parser = argparse.ArgumentParser(description="Generate static health.html")
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    storage = config["storage"]

    data_dir = PROJECT_ROOT / storage.get("data_dir", "data")
    classified_dir = PROJECT_ROOT / storage.get("classified_dir", "data/classified")
    db_path = PROJECT_ROOT / storage.get("database_path", "data/birds.db")

    # Disk
    disk = shutil.disk_usage(str(data_dir))
    disk_total_gb = round(disk.total / (1024**3), 1)
    disk_used_gb = round(disk.used / (1024**3), 1)
    disk_free_gb = round(disk.free / (1024**3), 1)
    disk_pct = round((disk.used / disk.total) * 100, 1)

    if disk_pct > 90:
        disk_color = "#f44336"
        disk_status = "CRITICAL"
    elif disk_pct > 75:
        disk_color = "#ff9800"
        disk_status = "WARNING"
    else:
        disk_color = "#4caf50"
        disk_status = "OK"

    # Photos
    captures_dir = data_dir / "captures"
    queue_count = len(list(captures_dir.glob("*.jpg"))) if captures_dir.exists() else 0

    species_rows = ""
    classified_count = 0
    if classified_dir.exists():
        for d in sorted(classified_dir.iterdir()):
            if d.is_dir() and not d.name.startswith("_"):
                count = len(list(d.glob("*.jpg")))
                classified_count += count
                name = d.name.replace("_", " ").title()
                species_rows += f"<tr><td>{name}</td><td>{count}</td></tr>\n"

    unclassified_dir = classified_dir / "_unclassified"
    unclassified_count = (
        len(list(unclassified_dir.glob("*.jpg"))) if unclassified_dir.exists() else 0
    )

    # Database
    total_sightings = 0
    unique_species = 0
    today_sightings = 0
    db_size_mb = 0
    recent_rows = ""

    if db_path.exists():
        db_size_mb = round(db_path.stat().st_size / (1024 * 1024), 2)
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            total_sightings = conn.execute("SELECT COUNT(*) as c FROM sightings").fetchone()["c"]
            unique_species = conn.execute(
                "SELECT COUNT(DISTINCT species) as c FROM sightings"
            ).fetchone()["c"]
            today = datetime.now().strftime("%Y-%m-%d")
            today_sightings = conn.execute(
                "SELECT COUNT(*) as c FROM sightings WHERE date = ?", (today,)
            ).fetchone()["c"]
            latest = conn.execute(
                "SELECT timestamp, species, confidence FROM sightings "
                "ORDER BY timestamp DESC LIMIT 10"
            ).fetchall()
            for r in latest:
                ts = r["timestamp"][:19].replace("T", " ")
                conf = f"{r['confidence'] * 100:.1f}%"
                recent_rows += f"<tr><td>{ts}</td><td>{r['species']}</td><td>{conf}</td></tr>\n"
            conn.close()
        except Exception as e:
            recent_rows = f'<tr><td colspan="3" style="color:red">DB error: {e}</td></tr>'

    if not recent_rows:
        recent_rows = '<tr><td colspan="3" style="color:#999">No sightings yet</td></tr>'
    if not species_rows:
        species_rows = '<tr><td colspan="2" style="color:#999">No species detected yet</td></tr>'

    # Log
    log_path = data_dir / "bird_feeder.log"
    log_size_mb = round(log_path.stat().st_size / (1024 * 1024), 2) if log_path.exists() else 0

    # Power metrics (current snapshot via vcgencmd / /sys)
    pm_cfg = config.get("power_monitoring", {})
    cpu_temp = None
    core_volts = None
    throttled = None
    power_history_rows = ""

    try:
        out = subprocess.check_output(
            ["vcgencmd", "measure_temp"], timeout=3, stderr=subprocess.DEVNULL, text=True
        )
        cpu_temp = round(float(out.strip().replace("temp=", "").replace("'C", "")), 1)
    except Exception:
        pass

    if cpu_temp is None:
        try:
            raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
            cpu_temp = round(int(raw) / 1000.0, 1)
        except Exception:
            pass

    try:
        out = subprocess.check_output(
            ["vcgencmd", "measure_volts", "core"], timeout=3, stderr=subprocess.DEVNULL, text=True
        )
        core_volts = round(float(out.strip().replace("volt=", "").replace("V", "")), 4)
    except Exception:
        pass

    try:
        out = subprocess.check_output(
            ["vcgencmd", "get_throttled"], timeout=3, stderr=subprocess.DEVNULL, text=True
        )
        throttled = out.strip().replace("throttled=", "")
    except Exception:
        pass

    # Read last 12 rows from power log CSV
    power_log_path = PROJECT_ROOT / pm_cfg.get("log_file", "data/power_log.csv")
    if power_log_path.exists():
        try:
            lines = power_log_path.read_text().splitlines()
            data_lines = [ln for ln in lines[1:] if ln.strip()]
            for line in reversed(data_lines[-12:]):
                parts = line.split(",")
                if len(parts) >= 3:
                    ts = parts[0]
                    t = f"{float(parts[1]):.1f}&deg;C" if parts[1] else "--"
                    v = f"{float(parts[2]):.4f} V" if parts[2] else "--"
                    power_history_rows += f"<tr><td>{ts}</td><td>{t}</td><td>{v}</td></tr>\n"
        except Exception:
            pass

    cpu_temp_str = f"{cpu_temp}&deg;C" if cpu_temp is not None else "N/A"
    core_volts_str = f"{core_volts} V" if core_volts is not None else "N/A"
    is_throttled = throttled and throttled != "0x0"
    throttle_str = (
        ("THROTTLED (" + throttled + ")") if is_throttled else ("OK" if throttled else "N/A")
    )
    temp_color = (
        "#f44336" if (cpu_temp or 0) > 80 else "#ff9800" if (cpu_temp or 0) > 70 else "#4caf50"
    )
    throttle_color = "#f44336" if is_throttled else "#4caf50"

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bird Feeder Health - {generated}</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           background: #f5f5f5; color: #333; padding: 1.5rem; }}
    h1 {{ color: #2e7d32; margin-bottom: 0.5rem; }}
    .ts {{ color: #999; font-size: 0.85rem; margin-bottom: 1.5rem; }}
    .card {{ background: white; border-radius: 8px; padding: 1.5rem;
            margin-bottom: 1rem; box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
    .card h2 {{ color: #2e7d32; margin-bottom: 1rem; font-size: 1.1rem; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 1rem; }}
    .metric {{ text-align: center; padding: 1rem; background: #f9f9f9; border-radius: 6px; }}
    .metric .value {{ font-size: 1.8rem; font-weight: bold; color: #2e7d32; }}
    .metric .label {{ font-size: 0.85rem; color: #666; margin-top: 0.3rem; }}
    .bar-bg {{ background: #e0e0e0; border-radius: 4px; height: 20px; margin-top: 0.5rem; }}
    .bar-fill {{ height: 100%; border-radius: 4px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 0.5rem; border-bottom: 1px solid #eee; }}
    th {{ color: #666; font-weight: 600; font-size: 0.85rem; }}
</style>
</head>
<body>
<h1>Bird Feeder System Health</h1>
<div class="ts">Generated: {generated}</div>

<div class="card">
    <h2>Disk Usage ({disk_status})</h2>
    <div class="grid">
        <div class="metric"><div class="value">{
        disk_free_gb
    } GB</div><div class="label">Free</div></div>
        <div class="metric"><div class="value">{
        disk_used_gb
    } GB</div><div class="label">Used</div></div>
        <div class="metric"><div class="value">{
        disk_total_gb
    } GB</div><div class="label">Total</div></div>
    </div>
    <div class="bar-bg">
        <div class="bar-fill" style="width:{disk_pct}%;background:{disk_color}"></div>
    </div>
    <div style="text-align:center;margin-top:4px;font-size:0.85rem;color:#666">{
        disk_pct
    }% used</div>
</div>

<div class="card">
    <h2>Photos</h2>
    <div class="grid">
        <div class="metric"><div class="value">{
        classified_count
    }</div><div class="label">Classified</div></div>
        <div class="metric"><div class="value">{
        queue_count
    }</div><div class="label">In Queue</div></div>
        <div class="metric"><div class="value">{
        unclassified_count
    }</div><div class="label">Unclassified</div></div>
    </div>
</div>

<div class="card">
    <h2>Species Breakdown</h2>
    <table>
        <tr><th>Species</th><th>Photos</th></tr>
        {species_rows}
    </table>
</div>

<div class="card">
    <h2>Database</h2>
    <div class="grid">
        <div class="metric"><div class="value">{
        total_sightings
    }</div><div class="label">Total Sightings</div></div>
        <div class="metric"><div class="value">{
        unique_species
    }</div><div class="label">Unique Species</div></div>
        <div class="metric"><div class="value">{
        today_sightings
    }</div><div class="label">Today</div></div>
        <div class="metric"><div class="value">{
        db_size_mb
    } MB</div><div class="label">DB Size</div></div>
    </div>
</div>

<div class="card">
    <h2>Recent Sightings</h2>
    <table>
        <tr><th>Time</th><th>Species</th><th>Confidence</th></tr>
        {recent_rows}
    </table>
</div>

<div class="card">
    <h2>Log File</h2>
    <div class="metric"><div class="value">{
        log_size_mb
    } MB</div><div class="label">Log Size</div></div>
</div>

<div class="card">
    <h2>Power &amp; Thermal</h2>
    <div class="grid">
        <div class="metric"><div class="value" style="color:{temp_color}">{
        cpu_temp_str
    }</div><div class="label">CPU Temperature</div></div>
        <div class="metric"><div class="value">{
        core_volts_str
    }</div><div class="label">Core Voltage</div></div>
        <div class="metric"><div class="value" style="color:{throttle_color}">{
        throttle_str
    }</div><div class="label">Throttle Status</div></div>
    </div>
    {
        f'''<details style="margin-top:1rem"><summary style="cursor:pointer;color:#666;font-size:0.9rem">Recent history</summary>
    <table style="margin-top:0.5rem"><tr><th>Time</th><th>Temp</th><th>Voltage</th></tr>
    {power_history_rows}</table></details>'''
        if power_history_rows
        else ""
    }
</div>
</body>
</html>"""

    output_path = data_dir / "health.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)
    print(f"Health report written to {output_path}")


if __name__ == "__main__":
    main()
