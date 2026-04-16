import requests
import threading
import asyncio
import aiohttp
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
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

# =========================================================
# API
# =========================================================
@app.route("/api/player_stats")
def api_player_stats():
    sort = request.args.get("sort", "season_avg")
    dir_ = request.args.get("dir", "desc")

    players = cached_players

    def key(p):
        return {
            "name": p["name"].lower(),
            "team": p["team"],
            "season_avg": p["season_avg"],
            "l5_avg": p["l5_avg"],
            "l10_avg": p["l10_avg"],
            "ab": p["ab"]
        }.get(sort, p["season_avg"])

    players.sort(key=key, reverse=(dir_ == "desc"))

    return jsonify({"players": players})

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
# UI (FULL RESTORED DASHBOARD)
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

<p>
<a href="/blown_leads" target="_blank">Blown Leads</a>
</p>

<table id="tbl">
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
<tbody></tbody>
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
    const res = await fetch(`/api/player_stats?sort=${sortField}&dir=${dir}`);
    const data = await res.json();

    const players = data.players;

    let l5 = players.map(p => p.l5_avg);
    let l10 = players.map(p => p.l10_avg);

    let l5min = Math.min(...l5), l5max = Math.max(...l5);
    let l10min = Math.min(...l10), l10max = Math.max(...l10);

    let tbody = document.querySelector("tbody");
    tbody.innerHTML = "";

    players.forEach(p => {
        let l5c = color(p.l5_avg, l5min, l5max);
        let l10c = color(p.l10_avg, l10min, l10max);

        tbody.innerHTML += `
        <tr>
            <td>${p.name}</td>
            <td>${p.team}</td>
            <td>${p.season_avg}</td>
            <td style="background:${l5c}">${p.l5_avg}</td>
            <td style="background:${l10c}">${p.l10_avg}</td>
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

@app.route("/blown_leads")
def blown_leads_page():
    rows = "".join([
        f"<tr><td>{i+1}</td><td>{t}</td><td>{v}</td></tr>"
        for i, (t, v) in enumerate(blown_leads_cache.items())
    ])

    return render_template_string(f"""
    <h2>Blown Leads</h2>
    <a href="/">Back</a>
    <table border=1>
    <tr><th>Rank</th><th>Team</th><th>Blown Leads</th></tr>
    {rows}
    </table>
    """)

# =========================================================
# START
# =========================================================
if __name__ == "__main__":
    threading.Thread(target=lambda: asyncio.run(load_all_players()), daemon=True).start()
    threading.Thread(target=blown_loop, daemon=True).start()

if __name__ == "__main__":
    app.run()
