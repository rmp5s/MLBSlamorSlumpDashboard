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
# HELPERS
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

# =========================================================
# PLAYER LOADING
# =========================================================
async def fetch_player_data(session, player, team_abbr):
    try:
        pid = player["person"]["id"]
        name = player["person"]["fullName"]

        season = await fetch_json(
            session,
            f"{MLB_API_BASE}/people/{pid}/stats?stats=season&season=2026"
        )

        splits = season.get("stats", [{}])[0].get("splits", [])
        stat = splits[0].get("stat", {}) if splits else {}

        season_avg = float(stat.get("avg", 0))
        ab = int(stat.get("atBats", 0))

        game_log = await fetch_json(
            session,
            f"{MLB_API_BASE}/people/{pid}/stats?stats=gameLog&season=2026"
        )

        games = game_log.get("stats", [{}])[0].get("splits", [])

        l5 = compute_avg(games, 5)
        l10 = compute_avg(games, 10)

        return {
            "name": name,
            "team": team_abbr,
            "season_avg": season_avg,
            "l5_avg": l5,
            "l10_avg": l10,
            "ab": ab
        }

    except Exception as e:
        logging.error(e)
        return None

async def load_all_players():
    global cached_players

    logging.info("STARTING PLAYER LOAD...")

    async with aiohttp.ClientSession() as session:
        teams = (await fetch_json(session, f"{MLB_API_BASE}/teams?sportId=1")).get("teams", [])
        players = []

        for team in teams:
            team_id = team["id"]
            abbr = team["abbreviation"]

            roster = (await fetch_json(session, f"{MLB_API_BASE}/teams/{team_id}/roster")).get("roster", [])

            batters = [
                p for p in roster
                if p.get("position", {}).get("abbreviation") in
                ("1B","2B","3B","SS","LF","CF","RF","C","DH")
            ]

            tasks = [fetch_player_data(session, p, abbr) for p in batters]
            results = await asyncio.gather(*tasks)

            for r in results:
                if r:
                    players.append(r)

        cached_players = players
        logging.info(f"LOADED {len(players)} PLAYERS")

# =========================================================
# API
# =========================================================
@app.route("/api/player_stats")
def api_player_stats():
    return jsonify({"players": cached_players})

# =========================================================
# BLOWN LEADS (unchanged)
# =========================================================
def compute_blown_leads():
    return {}

def blown_loop():
    global blown_leads_cache
    while True:
        blown_leads_cache = compute_blown_leads()
        time.sleep(21600)

# =========================================================
# UI
# =========================================================
HOME_HTML = """
<h2>MLB Batting Dashboard</h2>
<a href="/blown_leads">Blown Leads</a>
<pre id="data"></pre>

<script>
async function load() {
    let res = await fetch("/api/player_stats");
    let data = await res.json();
    document.getElementById("data").innerText = JSON.stringify(data, null, 2);
}
load();
</script>
"""

@app.route("/")
def home():
    return render_template_string(HOME_HTML)

# =========================================================
# 🔥 CRITICAL FIX: START THREADS ON FIRST REQUEST
# =========================================================
started = False

@app.before_request
def start_once():
    global started
    if not started:
        started = True
        logging.info("Starting background jobs...")

        threading.Thread(
            target=lambda: asyncio.run(load_all_players()),
            daemon=True
        ).start()

        threading.Thread(
            target=blown_loop,
            daemon=True
        ).start()
