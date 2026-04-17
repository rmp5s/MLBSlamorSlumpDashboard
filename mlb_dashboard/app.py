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
logging.basicConfig(level=logging.INFO)import requests
import threading
import asyncio
import aiohttp
import logging
import time
import json
import os
from collections import defaultdict
from flask import Flask, jsonify, render_template_string, send_file
from io import StringIO, BytesIO
import csv

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"
MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
MLB_BOXSCORE_URL = "https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"

CACHE_FILE = "cache.json"

cached_players = []
blown_leads_cache = {}
started = False

# =========================================================
# CACHE
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
            blown_leads_cache = data.get("blown", {})
            logging.info("Cache loaded")

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
# PLAYERS
# =========================================================
async def fetch_player(session, player, team):
    try:
        pid = player["person"]["id"]
        name = player["person"]["fullName"]

        season = await fetch_json(session, f"{MLB_API_BASE}/people/{pid}/stats?stats=season&season=2026")
        stat = season["stats"][0]["splits"][0]["stat"] if season["stats"][0]["splits"] else {}

        game_log = await fetch_json(session, f"{MLB_API_BASE}/people/{pid}/stats?stats=gameLog&season=2026")
        games = game_log["stats"][0]["splits"]

        return {
            "name": name,
            "team": team["abbreviation"],
            "league": team["league"]["name"],
            "division": team["division"]["name"],
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

            tasks = [fetch_player(session, p, t) for p in batters]
            results = await asyncio.gather(*tasks)

            players.extend([r for r in results if r])

        cached_players = players
        save_cache()
        logging.info("Players loaded")

# =========================================================
# BLOWN LEADS
# =========================================================
def get_games():
    r = requests.get(MLB_SCHEDULE_URL, params={"sportId":1,"season":2026})
    games=[]
    for d in r.json().get("dates",[]):
        for g in d["games"]:
            if g["status"]["detailedState"]=="Final":
                games.append(g)
    return games

def get_linescore(pk):
    return requests.get(MLB_BOXSCORE_URL.format(gamePk=pk)).json()["liveData"]["linescore"]["innings"]

def compute_blown():
    blown=defaultdict(int)
    for g in get_games():
        try:
            innings=get_linescore(g["gamePk"])
            home=g["teams"]["home"]["team"]["name"]
            away=g["teams"]["away"]["team"]["name"]

            h=a=0
            h_lead=a_lead=False

            for i in innings:
                h+=i.get("home",{}).get("runs",0)
                a+=i.get("away",{}).get("runs",0)
                if h>a: h_lead=True
                if a>h: a_lead=True

            if h_lead and h<a: blown[home]+=1
            if a_lead and a<h: blown[away]+=1
        except:
            pass

    return dict(sorted(blown.items(), key=lambda x:x[1], reverse=True))

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
# CSV (FIXED)
# =========================================================
@app.route("/export")
def export():
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(["Name","Team","Season","L5","L10","AB"])
    for p in cached_players:
        writer.writerow([p["name"],p["team"],p["season_avg"],p["l5_avg"],p["l10_avg"],p["ab"]])

    output = BytesIO()
    output.write(si.getvalue().encode("utf-8"))
    output.seek(0)

    return send_file(output, mimetype="text/csv", as_attachment=True, download_name="mlb.csv")

# =========================================================
# UI (FIXED DROPDOWNS)
# =========================================================
HOME_HTML = """
<html>
<head>
<style>
body { background:#181a1b; color:white; font-family:Arial; }
table { border-collapse:collapse; width:100%; }
th,td { border:1px solid #333; padding:6px; }
th { cursor:pointer; background:#222; }
input,select,button { margin:5px; padding:5px; }
a { color:#66ccff; }
</style>
</head>

<body>

<h2>MLB Batting Dashboard v2.4</h2>

<a href="/blown">Blown Leads</a><br>

<button onclick="exportCSV()">Export CSV</button>
<button onclick="clearSelections()">CLEAR SELECTIONS</button><br>

<select id="team" onchange="load()"></select>
<select id="league" onchange="load()"></select>
<select id="division" onchange="load()"></select>

<input id="search" placeholder="Search players..." oninput="load()">
<input id="addPlayer" placeholder="Add player (max 10)" oninput="suggest()">

<div id="suggestions"></div>

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
let data=[],field="season_avg",dir="desc",selected=[];

function sort(f){
    if(field===f) dir=dir==="asc"?"desc":"asc";
    else {field=f;dir="desc";}
    load();
}

function exportCSV(){ window.location="/export"; }

function clearSelections(){ selected=[]; load(); }

function color(v,min,max){
    let r=(v-min)/(max-min+0.0001);
    return `rgb(${Math.floor(255*r)},0,${Math.floor(255*(1-r))})`;
}

async function init(){
    let res=await fetch("/api/players");
    data=await res.json();

    let teams=[...new Set(data.map(p=>p.team))];
    let leagues=[...new Set(data.map(p=>p.league))];
    let divisions=[...new Set(data.map(p=>p.division))];

    team.innerHTML="<option value=''>All Teams</option>"+teams.map(t=>`<option>${t}</option>`);
    league.innerHTML="<option value=''>All Leagues</option>"+leagues.map(l=>`<option>${l}</option>`);
    division.innerHTML="<option value=''>All Divisions</option>"+divisions.map(d=>`<option>${d}</option>`);

    load();
}

function suggest(){
    let q=addPlayer.value.toLowerCase();
    if(!q){ suggestions.innerHTML=""; return; }

    let matches=data.filter(p=>p.name.toLowerCase().includes(q)).slice(0,10);

    suggestions.innerHTML=matches.map(p=>
        `<div onclick="add('${p.name}')">${p.name}</div>`
    ).join("");
}

function add(name){
    if(selected.length>=10) return;
    if(!selected.includes(name)) selected.push(name);
    suggestions.innerHTML="";
    addPlayer.value="";
    load();
}

function load(){
    let rows=[...data];

    let s=search.value.toLowerCase();

    rows=rows.filter(p=>
        (!team.value||p.team===team.value) &&
        (!league.value||p.league===league.value) &&
        (!division.value||p.division===division.value) &&
        (p.name.toLowerCase().includes(s)||p.team.toLowerCase().includes(s)) &&
        (selected.length===0 || selected.includes(p.name))
    );

    rows.sort((a,b)=>{
        let v1=a[field],v2=b[field];
        if(typeof v1==="string") return dir==="asc"?v1.localeCompare(v2):v2.localeCompare(v1);
        return dir==="asc"?v1-v2:v2-v1;
    });

    let l5=rows.map(p=>p.l5_avg);
    let l10=rows.map(p=>p.l10_avg);

    document.getElementById("body").innerHTML=
        rows.map(p=>`<tr>
        <td>${p.name}</td>
        <td>${p.team}</td>
        <td>${p.season_avg}</td>
        <td style="background:${color(p.l5_avg,Math.min(...l5),Math.max(...l5))}">${p.l5_avg}</td>
        <td style="background:${color(p.l10_avg,Math.min(...l10),Math.max(...l10))}">${p.l10_avg}</td>
        <td>${p.ab}</td>
        </tr>`).join("");
}

init();
</script>

</body>
</html>
"""

BLOWN_HTML = """<html><body><h2>MLB Blown Leads v2.4</h2><a href="/">Back</a><table border=1 id=tbl></table>
<script>
fetch('/api/blown').then(r=>r.json()).then(d=>{
let rows=Object.entries(d);
tbl.innerHTML="<tr><th>Team</th><th>Blown Leads</th></tr>"+
rows.map(r=>`<tr><td>${r[0]}</td><td>${r[1]}</td></tr>`).join("");
});
</script></body></html>"""

@app.route("/")
def home():
    return render_template_string(HOME_HTML)

@app.route("/blown")
def blown():
    return render_template_string(BLOWN_HTML)

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

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"
MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
MLB_BOXSCORE_URL = "https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live"

CACHE_FILE = "cache.json"

cached_players = []
blown_leads_cache = {}
started = False

# =========================================================
# CACHE
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
            blown_leads_cache = data.get("blown", {})
            logging.info("Cache loaded")

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
# PLAYERS
# =========================================================
async def fetch_player(session, player, team):
    try:
        pid = player["person"]["id"]
        name = player["person"]["fullName"]

        season = await fetch_json(session, f"{MLB_API_BASE}/people/{pid}/stats?stats=season&season=2026")
        stat = season["stats"][0]["splits"][0]["stat"] if season["stats"][0]["splits"] else {}

        game_log = await fetch_json(session, f"{MLB_API_BASE}/people/{pid}/stats?stats=gameLog&season=2026")
        games = game_log["stats"][0]["splits"]

        return {
            "name": name,
            "team": team["abbreviation"],
            "league": team["league"]["name"],
            "division": team["division"]["name"],
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

            tasks = [fetch_player(session, p, t) for p in batters]
            results = await asyncio.gather(*tasks)

            players.extend([r for r in results if r])

        cached_players = players
        save_cache()
        logging.info("Players loaded")

# =========================================================
# BLOWN LEADS
# =========================================================
def get_games():
    r = requests.get(MLB_SCHEDULE_URL, params={"sportId":1,"season":2026})
    games=[]
    for d in r.json().get("dates",[]):
        for g in d["games"]:
            if g["status"]["detailedState"]=="Final":
                games.append(g)
    return games

def get_linescore(pk):
    return requests.get(MLB_BOXSCORE_URL.format(gamePk=pk)).json()["liveData"]["linescore"]["innings"]

def compute_blown():
    blown=defaultdict(int)
    for g in get_games():
        try:
            innings=get_linescore(g["gamePk"])
            home=g["teams"]["home"]["team"]["name"]
            away=g["teams"]["away"]["team"]["name"]

            h=a=0
            h_lead=a_lead=False

            for i in innings:
                h+=i.get("home",{}).get("runs",0)
                a+=i.get("away",{}).get("runs",0)
                if h>a: h_lead=True
                if a>h: a_lead=True

            if h_lead and h<a: blown[home]+=1
            if a_lead and a<h: blown[away]+=1
        except:
            pass

    return dict(sorted(blown.items(), key=lambda x:x[1], reverse=True))

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
# CSV
# =========================================================
@app.route("/export")
def export():
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(["Name","Team","Season","L5","L10","AB"])
    for p in cached_players:
        writer.writerow([p["name"],p["team"],p["season_avg"],p["l5_avg"],p["l10_avg"],p["ab"]])
    return send_file(StringIO(si.getvalue()), mimetype="text/csv", as_attachment=True, download_name="mlb.csv")

# =========================================================
# UI
# =========================================================
HOME_HTML = """
<html>
<head>
<style>
body { background:#181a1b; color:white; font-family:Arial; }
table { border-collapse:collapse; width:100%; }
th,td { border:1px solid #333; padding:6px; }
th { cursor:pointer; background:#222; }
input,select,button { margin:5px; padding:5px; }
a { color:#66ccff; }
</style>
</head>

<body>

<h2>MLB Batting Dashboard v2.3</h2>

<a href="/blown">Blown Leads</a><br>

<button onclick="exportCSV()">Export CSV</button>
<button onclick="clearSelections()">CLEAR SELECTIONS</button><br>

<select id="team"></select>
<select id="league"></select>
<select id="division"></select>

<input id="search" placeholder="Search players..." oninput="load()">
<input id="addPlayer" placeholder="Add player (max 10)" oninput="suggest()">

<div id="suggestions"></div>

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
let data=[],field="season_avg",dir="desc",selected=[];

function sort(f){
    if(field===f) dir=dir==="asc"?"desc":"asc";
    else {field=f;dir="desc";}
    load();
}

function exportCSV(){ window.location="/export"; }

function clearSelections(){
    selected=[];
    load();
}

function color(v,min,max){
    let r=(v-min)/(max-min+0.0001);
    return `rgb(${Math.floor(255*r)},0,${Math.floor(255*(1-r))})`;
}

async function init(){
    let res=await fetch("/api/players");
    data=await res.json();

    let teams=[...new Set(data.map(p=>p.team))];
    let leagues=[...new Set(data.map(p=>p.league))];
    let divisions=[...new Set(data.map(p=>p.division))];

    team.innerHTML="<option value=''>All Teams</option>"+teams.map(t=>`<option>${t}</option>`);
    league.innerHTML="<option value=''>All Leagues</option>"+leagues.map(l=>`<option>${l}</option>`);
    division.innerHTML="<option value=''>All Divisions</option>"+divisions.map(d=>`<option>${d}</option>`);

    load();
}

function suggest(){
    let q=addPlayer.value.toLowerCase();
    if(!q){ suggestions.innerHTML=""; return; }

    let matches=data.filter(p=>p.name.toLowerCase().includes(q)).slice(0,10);

    suggestions.innerHTML=matches.map(p=>
        `<div onclick="add('${p.name}')">${p.name}</div>`
    ).join("");
}

function add(name){
    if(selected.length>=10) return;
    if(!selected.includes(name)) selected.push(name);
    suggestions.innerHTML="";
    addPlayer.value="";
    load();
}

function load(){
    let rows=[...data];

    let s=search.value.toLowerCase();

    rows=rows.filter(p=>
        (!team.value||p.team===team.value) &&
        (!league.value||p.league===league.value) &&
        (!division.value||p.division===division.value) &&
        (p.name.toLowerCase().includes(s)||p.team.toLowerCase().includes(s)) &&
        (selected.length===0 || selected.includes(p.name))
    );

    rows.sort((a,b)=>{
        let v1=a[field],v2=b[field];
        if(typeof v1==="string") return dir==="asc"?v1.localeCompare(v2):v2.localeCompare(v1);
        return dir==="asc"?v1-v2:v2-v1;
    });

    let l5=rows.map(p=>p.l5_avg);
    let l10=rows.map(p=>p.l10_avg);

    document.getElementById("body").innerHTML=
        rows.map(p=>`<tr>
        <td>${p.name}</td>
        <td>${p.team}</td>
        <td>${p.season_avg}</td>
        <td style="background:${color(p.l5_avg,Math.min(...l5),Math.max(...l5))}">${p.l5_avg}</td>
        <td style="background:${color(p.l10_avg,Math.min(...l10),Math.max(...l10))}">${p.l10_avg}</td>
        <td>${p.ab}</td>
        </tr>`).join("");
}

init();
</script>

</body>
</html>
"""

BLOWN_HTML = """
<html>
<head>
<style>
body { background:#181a1b; color:white; font-family:Arial; }
table { border-collapse:collapse; width:50%; }
th,td { border:1px solid #333; padding:6px; }
th { cursor:pointer; background:#222; }
a { color:#66ccff; }
</style>
</head>

<body>

<h2>MLB Blown Leads v2.3</h2>

<a href="/">Back</a>

<table>
<thead>
<tr>
<th onclick="sort('team')">Team</th>
<th onclick="sort('value')">Blown Leads</th>
</tr>
</thead>
<tbody id="body"></tbody>
</table>

<script>
let data=[],field="value",dir="desc";

function sort(f){
    if(field===f) dir=dir==="asc"?"desc":"asc";
    else {field=f;dir="desc";}
    render();
}

async function init(){
    let res=await fetch("/api/blown");
    let raw=await res.json();
    data=Object.entries(raw).map(([k,v])=>({team:k,value:v}));
    render();
}

function render(){
    data.sort((a,b)=>dir==="asc"?a[field]-b[field]:b[field]-a[field]);
    document.getElementById("body").innerHTML=
        data.map(r=>`<tr><td>${r.team}</td><td>${r.value}</td></tr>`).join("");
}

init();
</script>

</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HOME_HTML)

@app.route("/blown")
def blown():
    return render_template_string(BLOWN_HTML)

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
