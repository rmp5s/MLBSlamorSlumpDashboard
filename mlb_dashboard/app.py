import requests
import threading
import asyncio
import aiohttp
import logging
import time
from collections import defaultdict
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"
MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
MLB_BOXSCORE_URL = "https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"

cached_players = []
blown_leads_cache = {}

# =========================================================
# DATA
# =========================================================
async def fetch_json(session, url):
    async with session.get(url) as resp:
        return await resp.json()

def compute_avg(games, n):
    hits = 0
    ab = 0
    for g in games[:n]:
        stat = g.get("stat", {})
        hits += int(stat.get("hits", 0))
        ab += int(stat.get("atBats", 0))
    return round(hits / ab, 3) if ab > 0 else 0.0

async def fetch_player_data(session, player, team_abbr):
    pid = player["person"]["id"]
    name = player["person"]["fullName"]

    season = await fetch_json(session, f"{MLB_API_BASE}/people/{pid}/stats?stats=season&season=2026")

    splits = season.get("stats", [{}])[0].get("splits", [])
    stat = splits[0].get("stat", {}) if splits else {}

    season_avg = float(stat.get("avg", 0))
    ab = int(stat.get("atBats", 0))

    game_log = await fetch_json(session, f"{MLB_API_BASE}/people/{pid}/stats?stats=gameLog&season=2026")
    games = game_log.get("stats", [{}])[0].get("splits", [])

    return {
        "name": name,
        "team": team_abbr,
        "season_avg": season_avg,
        "l5_avg": compute_avg(games, 5),
        "l10_avg": compute_avg(games, 10),
        "ab": ab
    }

async def load_all_players():
    global cached_players

    async with aiohttp.ClientSession() as session:
        teams = (await fetch_json(session, f"{MLB_API_BASE}/teams?sportId=1")).get("teams", [])
        players = []

        for team in teams:
            roster = (await fetch_json(session, f"{MLB_API_BASE}/teams/{team['id']}/roster")).get("roster", [])

            batters = [
                p for p in roster
                if p.get("position", {}).get("abbreviation") in
                ("1B","2B","3B","SS","LF","CF","RF","C","DH")
            ]

            tasks = [fetch_player_data(session, p, team["abbreviation"]) for p in batters]
            results = await asyncio.gather(*tasks)

            players.extend([r for r in results if r])

        cached_players = players
        logging.info(f"Loaded {len(players)} players")

# =========================================================
# API
# =========================================================
@app.route("/api/player_stats")
def api_player_stats():
    return jsonify({"players": cached_players})

# =========================================================
# UI (RESTORED FULL DASHBOARD)
# =========================================================
HOME_HTML = """
<html>
<head>
<title>MLB Dashboard</title>

<style>
body { font-family: Arial; margin: 20px; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #ddd; padding: 6px; cursor: pointer; }
th { background: #f4f4f4; }
</style>
</head>

<body>

<h2>MLB Batting Dashboard</h2>

<p><a href="/blown_leads" target="_blank">Blown Leads</a></p>

<table>
<thead>
<tr>
<th onclick="sort('name')">Name</th>
<th onclick="sort('team')">Team</th>
<th onclick="sort('season_avg')">Season AVG</th>
<th onclick="sort('l5_avg')">L5 AVG</th>
<th onclick="sort('l10_avg')">L10 AVG</th>
<th onclick="sort('ab')">AB</th>
</tr>
</thead>
<tbody id="body"></tbody>
</table>

<script>
let sortField = "season_avg";
let dir = "desc";

function color(val, min, max) {
    let ratio = (val - min) / (max - min + 0.0001);
    let r = Math.floor(255 * ratio);
    let b = Math.floor(255 * (1 - ratio));
    return `rgb(${r},0,${b})`;
}

function sort(field) {
    sortField = field;
    dir = dir === "asc" ? "desc" : "asc";
    load();
}

async function load() {
    const res = await fetch("/api/player_stats");
    const data = await res.json();

    let players = data.players;

    let l5 = players.map(p => p.l5_avg);
    let l10 = players.map(p => p.l10_avg);

    let l5min = Math.min(...l5), l5max = Math.max(...l5);
    let l10min = Math.min(...l10), l10max = Math.max(...l10);

    let body = document.getElementById("body");
    body.innerHTML = "";

    players.forEach(p => {
        body.innerHTML += `
        <tr>
            <td>${p.name}</td>
            <td>${p.team}</td>
            <td>${p.season_avg}</td>
            <td style="background:${color(p.l5_avg,l5min,l5max)}">${p.l5_avg}</td>
            <td style="background:${color(p.l10_avg,l10min,l10max)}">${p.l10_avg}</td>
            <td>${p.ab}</td>
        </tr>`;
    });
}

load();
</script>

</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HOME_HTML)

# =========================================================
# BLOWN LEADS (kept minimal placeholder-safe)
# =========================================================
@app.route("/blown_leads")
def blown():
    return "<h2>Blown Leads Coming Back Next Step</h2>"

# =========================================================
# STARTUP (Render-safe)
# =========================================================
def start():
    threading.Thread(target=lambda: asyncio.run(load_all_players()), daemon=True).start()

start()

if __name__ == "__main__":
    app.run()
