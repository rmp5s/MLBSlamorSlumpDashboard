import requests
import threading
import asyncio
import aiohttp
import logging
import time
from collections import defaultdict
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"
MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
MLB_BOXSCORE_URL = "https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"

cached_players = []
blown_leads_cache = {}
started = False

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
# PLAYER DATA
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

        return {
            "name": name,
            "team": team_abbr,
            "season_avg": season_avg,
            "l5_avg": compute_avg(games, 5),
            "l10_avg": compute_avg(games, 10),
            "ab": ab
        }

    except Exception as e:
        logging.error(e)
        return None

async def load_all_players():
    global cached_players

    logging.info("Loading player data...")

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
# BLOWN LEADS
# =========================================================
def get_all_games(season=2026):
    r = requests.get(MLB_SCHEDULE_URL, params={
        "sportId": 1,
        "season": season,
        "gameType": "R"
    })
    data = r.json()

    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            if g["status"]["detailedState"] == "Final":
                games.append({
                    "gamePk": g["gamePk"],
                    "home": g["teams"]["home"]["team"]["name"],
                    "away": g["teams"]["away"]["team"]["name"]
                })
    return games

def get_linescore(gamePk):
    r = requests.get(MLB_BOXSCORE_URL.format(gamePk=gamePk))
    return r.json().get("liveData", {}).get("linescore", {}).get("innings", [])

def did_team_blow_lead(innings, side):
    home = away = 0
    had_lead = False

    for i in innings:
        home += i.get("home", {}).get("runs", 0)
        away += i.get("away", {}).get("runs", 0)

        if side == "home" and home > away:
            had_lead = True
        if side == "away" and away > home:
            had_lead = True

    return had_lead and ((side == "home" and home < away) or (side == "away" and away < home))

def compute_blown_leads():
    games = get_all_games(2026)
    blown = defaultdict(int)

    for g in games:
        try:
            innings = get_linescore(g["gamePk"])

            if did_team_blow_lead(innings, "home"):
                blown[g["home"]] += 1
            if did_team_blow_lead(innings, "away"):
                blown[g["away"]] += 1

        except Exception as e:
            logging.error(e)

    return dict(sorted(blown.items(), key=lambda x: x[1], reverse=True))

def blown_loop():
    global blown_leads_cache
    while True:
        blown_leads_cache = compute_blown_leads()
        time.sleep(21600)

# =========================================================
# API
# =========================================================
@app.route("/api/player_stats")
def api_player_stats():
    return jsonify({"players": cached_players})

@app.route("/api/blown_leads")
def api_blown_leads():
    return jsonify(blown_leads_cache)

# =========================================================
# UI
# =========================================================
HOME_HTML = """
<html>
<head>
<title>MLB Dashboard</title>

<style>
body { font-family: Arial; margin: 20px; background:#d3d3d3; color:white; }
table { border-collapse: collapse; width: 100%; background:white; color:black; }
th, td { border: 1px solid #ddd; padding: 6px; }
th { background: #444; color:white; }
a { color: blue; }
</style>
</head>

<body>

<h2>MLB Batting Dashboard v1.1</h2>

<p><a href="/blown_leads" target="_blank">Blown Leads</a></p>

<table>
<thead>
<tr>
<th>Name</th>
<th>Team</th>
<th>Season AVG</th>
<th>L5 AVG</th>
<th>L10 AVG</th>
<th>AB</th>
</tr>
</thead>
<tbody id="body"></tbody>
</table>

<script>
function color(val, min, max) {
    let ratio = (val - min) / (max - min + 0.0001);
    let r = Math.floor(255 * ratio);
    let b = Math.floor(255 * (1 - ratio));
    return `rgb(${r},0,${b})`;
}

async function load() {
    const res = await fetch("/api/player_stats");
    const data = await res.json();

    let players = data.players;
    let body = document.getElementById("body");

    if (!players || players.length === 0) {
        body.innerHTML = "<tr><td colspan='6'>Loading data...</td></tr>";
        setTimeout(load, 2000);
        return;
    }

    let l5 = players.map(p => p.l5_avg);
    let l10 = players.map(p => p.l10_avg);

    let l5min = Math.min(...l5), l5max = Math.max(...l5);
    let l10min = Math.min(...l10), l10max = Math.max(...l10);

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

BLOWN_HTML = """
<html>
<head>
<title>Blown Leads</title>

<style>
body { font-family: Arial; margin: 20px; background:#d3d3d3; color:white; }
table { border-collapse: collapse; width: 50%; background:white; color:black; }
th, td { border: 1px solid #ddd; padding: 6px; }
th { background: #444; color:white; }
</style>
</head>

<body>

<h2>MLB Blown Leads v1.1</h2>
<p><a href="/">Back</a></p>

<table>
<thead>
<tr><th>Rank</th><th>Team</th><th>Blown Leads</th></tr>
</thead>
<tbody id="body"></tbody>
</table>

<script>
async function load() {
    const res = await fetch("/api/blown_leads");
    const data = await res.json();

    let body = document.getElementById("body");

    let entries = Object.entries(data);

    if (entries.length === 0) {
        body.innerHTML = "<tr><td colspan='3'>Loading data...</td></tr>";
        setTimeout(load, 2000);
        return;
    }

    body.innerHTML = "";

    entries.forEach((e, i) => {
        body.innerHTML += `
        <tr>
            <td>${i+1}</td>
            <td>${e[0]}</td>
            <td>${e[1]}</td>
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

@app.route("/blown_leads")
def blown_page():
    return render_template_string(BLOWN_HTML)

# =========================================================
# STARTUP (RENDER SAFE)
# =========================================================
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

if __name__ == "__main__":
    app.run()
