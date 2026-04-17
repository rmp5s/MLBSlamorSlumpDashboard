import requests
import threading
import asyncio
import aiohttp
import logging
import time
import json
import os
from collections import defaultdict
from flask import Flask, jsonify, render_template_string, send_file
from io import StringIO
import csv

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"
CACHE_FILE = "cache.json"

cached_players = []
blown_leads_cache = {}
started = False

# =========================================================
# CACHE (INSTANT LOAD)
# =========================================================
def save_cache():
    with open(CACHE_FILE, "w") as f:
        json.dump({
            "players": cached_players,
            "blown": blown_leads_cache
        }, f)

def load_cache():
    global cached_players, blown_leads_cache
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            data = json.load(f)
            cached_players = data.get("players", [])
            blown_leads_cache = data.get("blown", [])
            logging.info("Loaded cache from disk")

# =========================================================
# HELPERS
# =========================================================
async def fetch_json(session, url):
    async with session.get(url) as resp:
        return await resp.json()

def compute_avg(games, n):
    hits = sum(int(g.get("stat", {}).get("hits", 0)) for g in games[:n])
    ab = sum(int(g.get("stat", {}).get("atBats", 0)) for g in games[:n])
    return round(hits / ab, 3) if ab > 0 else 0.0

# =========================================================
# PLAYER LOAD
# =========================================================
async def fetch_player(session, player, team_abbr):
    try:
        pid = player["person"]["id"]
        name = player["person"]["fullName"]

        season = await fetch_json(session, f"{MLB_API_BASE}/people/{pid}/stats?stats=season&season=2026")
        stat = season["stats"][0]["splits"][0]["stat"] if season["stats"][0]["splits"] else {}

        game_log = await fetch_json(session, f"{MLB_API_BASE}/people/{pid}/stats?stats=gameLog&season=2026")
        games = game_log["stats"][0]["splits"]

        return {
            "name": name,
            "team": team_abbr,
            "season_avg": float(stat.get("avg", 0)),
            "l5_avg": compute_avg(games, 5),
            "l10_avg": compute_avg(games, 10),
            "ab": int(stat.get("atBats", 0))
        }
    except:
        return None

async def load_players():
    global cached_players
    async with aiohttp.ClientSession() as session:
        teams = (await fetch_json(session, f"{MLB_API_BASE}/teams?sportId=1"))["teams"]

        players = []
        for t in teams:
            roster = (await fetch_json(session, f"{MLB_API_BASE}/teams/{t['id']}/roster"))["roster"]

            batters = [p for p in roster if p["position"]["abbreviation"] in
                       ("1B","2B","3B","SS","LF","CF","RF","C","DH")]

            tasks = [fetch_player(session, p, t["abbreviation"]) for p in batters]
            results = await asyncio.gather(*tasks)

            players.extend([r for r in results if r])

        cached_players = players
        save_cache()
        logging.info("Players refreshed")

# =========================================================
# BLOWN LEADS
# =========================================================
def compute_blown():
    return blown_leads_cache  # keep simple for now

def blown_loop():
    global blown_leads_cache
    while True:
        blown_leads_cache = compute_blown()
        save_cache()
        time.sleep(21600)

# =========================================================
# API
# =========================================================
@app.route("/api/players")
def api_players():
    return jsonify(cached_players)

@app.route("/api/blown")
def api_blown():
    return jsonify(blown_leads_cache)

# =========================================================
# CSV EXPORT
# =========================================================
@app.route("/export")
def export():
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(["Name","Team","Season","L5","L10","AB"])

    for p in cached_players:
        writer.writerow([p["name"], p["team"], p["season_avg"], p["l5_avg"], p["l10_avg"], p["ab"]])

    output = StringIO(si.getvalue())
    return send_file(
        output,
        mimetype="text/csv",
        as_attachment=True,
        download_name="mlb_stats.csv"
    )

# =========================================================
# UI
# =========================================================
HTML = """
<html>
<head>
<style>
body { background:#181a1b; color:white; font-family:Arial; }
table { border-collapse:collapse; width:100%; }
th,td { border:1px solid #333; padding:6px; }
th { cursor:pointer; background:#222; }
select,input,button { margin:5px; padding:5px; }
</style>
</head>

<body>

<h2>MLB Batting Dashboard v2.0</h2>

<button onclick="exportCSV()">Export CSV</button>

<br>

<select id="team"></select>
<select id="league">
<option value="">All Leagues</option>
<option>AL</option>
<option>NL</option>
</select>
<select id="division">
<option value="">All Divisions</option>
<option>East</option>
<option>Central</option>
<option>West</option>
</select>

<input id="search" placeholder="Search players..." oninput="load()">

<table>
<thead>
<tr>
<th onclick="sort('name')">Name</th>
<th onclick="sort('team')">Team</th>
<th onclick="sort('season_avg')">Season</th>
<th onclick="sort('l5_avg')">L5</th>
<th onclick="sort('l10_avg')">L10</th>
<th onclick="sort('ab')">AB</th>
</tr>
</thead>
<tbody id="body"></tbody>
</table>

<script>
let data=[];
let field="season_avg",dir="desc";

function sort(f){
    if(field===f) dir=dir==="asc"?"desc":"asc";
    else {field=f;dir="desc";}
    load();
}

function exportCSV(){
    window.location="/export";
}

async function init(){
    let res=await fetch("/api/players");
    data=await res.json();

    let teams=[...new Set(data.map(p=>p.team))].sort();
    let sel=document.getElementById("team");
    sel.innerHTML="<option value=''>All Teams</option>"+teams.map(t=>`<option>${t}</option>`).join("");

    load();
}

function load(){
    let rows=[...data];

    let search=document.getElementById("search").value.toLowerCase();
    let team=document.getElementById("team").value;

    rows=rows.filter(p=>
        (!team||p.team===team) &&
        (p.name.toLowerCase().includes(search)||p.team.toLowerCase().includes(search))
    );

    rows.sort((a,b)=>{
        let v1=a[field],v2=b[field];
        if(typeof v1==="string") return dir==="asc"?v1.localeCompare(v2):v2.localeCompare(v1);
        return dir==="asc"?v1-v2:v2-v1;
    });

    document.getElementById("body").innerHTML=
        rows.map(p=>`<tr>
        <td>${p.name}</td>
        <td>${p.team}</td>
        <td>${p.season_avg}</td>
        <td>${p.l5_avg}</td>
        <td>${p.l10_avg}</td>
        <td>${p.ab}</td>
        </tr>`).join("");
}

init();
</script>

</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

# =========================================================
# STARTUP
# =========================================================
load_cache()

@app.before_request
def start():
    global started
    if not started:
        started=True
        threading.Thread(target=lambda: asyncio.run(load_players()), daemon=True).start()
        threading.Thread(target=blown_loop, daemon=True).start()

if __name__ == "__main__":
    app.run()
