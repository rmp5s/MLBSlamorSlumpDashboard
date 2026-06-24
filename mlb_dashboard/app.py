Understood. Here is the **complete `app.py`**. This version is Render-safe: it starts the MLB loader in a true background thread from the request path, does **not** block `/api/players`, shows partial results while loading, and includes `/healthz`.

```python
import asyncio
import logging
import os
import socket
import sys
import threading
import time
from datetime import datetime, timedelta

import aiohttp
from flask import Flask, jsonify, request

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"
REFRESH_SECONDS = 6 * 60 * 60
LOOKBACK_DAYS = 90
MAX_CONCURRENT_REQUESTS = 6

cached_players = []
cache_loaded = False
cache_loading = False
last_updated = None
last_load_error = None
load_progress = "Not started"

cache_lock = threading.Lock()
loader_lock = threading.Lock()
loader_thread = None


def format_avg(avg):
    if avg is None:
        return ".000"
    return ".%03d" % int(round(avg * 1000))


def calc_avg(hits, at_bats):
    return hits / at_bats if at_bats > 0 else None


def avg_from_games(games):
    hits = sum(int(g.get("hits", 0)) for g in games)
    at_bats = sum(int(g.get("atBats", 0)) for g in games)
    return calc_avg(hits, at_bats)


async def fetch_json(session, sem, url, params=None):
    async with sem:
        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logging.warning("HTTP %s for %s", resp.status, url)
                    return {}
                return await resp.json()
        except Exception as e:
            logging.warning("Fetch error for %s: %s", url, e)
            return {}


async def fetch_season_stat(session, sem, player_id, season_year):
    url = f"{MLB_API_BASE}/people/{player_id}/stats"
    params = {
        "stats": "season",
        "group": "hitting",
        "season": str(season_year),
    }

    data = await fetch_json(session, sem, url, params)
    stats_list = data.get("stats", [])
    if not stats_list:
        return None

    splits = stats_list[0].get("splits", [])
    if not splits:
        return None

    return splits[0].get("stat", {})


async def get_recent_team_games(session, sem, team_id):
    end = datetime.utcnow()
    start = end - timedelta(days=LOOKBACK_DAYS)

    url = f"{MLB_API_BASE}/schedule"
    params = {
        "sportId": 1,
        "teamId": team_id,
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate": end.strftime("%Y-%m-%d"),
    }

    data = await fetch_json(session, sem, url, params)

    games = []
    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            status = game.get("status", {}).get("detailedState", "")
            if status in ("Final", "Game Over", "Completed Early"):
                games.append({
                    "gamePk": game["gamePk"],
                    "gameDate": game.get("gameDate", ""),
                })

    games.sort(key=lambda g: g["gameDate"], reverse=True)
    return games[:25]


async def fetch_boxscore(session, sem, game_pk):
    url = f"{MLB_API_BASE}/game/{game_pk}/boxscore"
    return await fetch_json(session, sem, url)


def get_player_batting_from_boxscore(boxscore, player_id):
    teams = boxscore.get("teams", {})
    all_players = {}

    all_players.update(teams.get("home", {}).get("players", {}))
    all_players.update(teams.get("away", {}).get("players", {}))

    batting = all_players.get(f"ID{player_id}", {}).get("stats", {}).get("batting")
    if not batting:
        return None

    return {
        "hits": int(batting.get("hits", 0)),
        "atBats": int(batting.get("atBats", 0)),
    }


async def process_team(session, sem, team, season_year, team_index, team_total):
    global load_progress

    team_id = team["id"]
    team_abbr = team["abbreviation"]

    with cache_lock:
        load_progress = f"Loading {team_abbr} ({team_index}/{team_total})"

    logging.info(load_progress)

    roster_url = f"{MLB_API_BASE}/teams/{team_id}/roster"
    roster_data = await fetch_json(session, sem, roster_url)
    roster = roster_data.get("roster", [])

    batters = [
        p for p in roster
        if p.get("position", {}).get("abbreviation") != "P"
    ]

    recent_games = await get_recent_team_games(session, sem, team_id)

    boxscores = []
    for game in recent_games:
        boxscores.append(await fetch_boxscore(session, sem, game["gamePk"]))

    team_players = []

    for player in batters:
        person = player["person"]
        player_id = person["id"]

        season_stat = await fetch_season_stat(session, sem, player_id, season_year)
        if not season_stat:
            continue

        season_ab = int(season_stat.get("atBats", 0))
        season_hits = int(season_stat.get("hits", 0))
        season_avg = calc_avg(season_hits, season_ab)

        if season_ab < 50:
            continue

        recent_player_games = []

        for box in boxscores:
            batting = get_player_batting_from_boxscore(box, player_id)
            if batting is not None:
                recent_player_games.append(batting)

            if len(recent_player_games) >= 10:
                break

        l5_avg = avg_from_games(recent_player_games[:5])
        l10_avg = avg_from_games(recent_player_games[:10])

        team_players.append({
            "name": person["fullName"],
            "team": team_abbr,
            "season_avg": season_avg,
            "ab": season_ab,
            "l5_avg": l5_avg,
            "l10_avg": l10_avg,
            "l5": format_avg(l5_avg),
            "l10": format_avg(l10_avg),
            "season": format_avg(season_avg),
            "diff_l5": (l5_avg or 0) - (season_avg or 0),
            "diff_l10": (l10_avg or 0) - (season_avg or 0),
        })

    logging.info("%s: loaded %s players", team_abbr, len(team_players))
    return team_players


async def load_all_players():
    global cached_players, cache_loaded, cache_loading, last_updated, last_load_error, load_progress

    with cache_lock:
        if cache_loading:
            logging.info("Load already in progress; skipping duplicate load.")
            return

        cache_loading = True
        last_load_error = None
        load_progress = "Fetching MLB teams..."

    try:
        logging.info("Starting MLB data refresh...")

        season_year = datetime.utcnow().year
        sem = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        timeout = aiohttp.ClientTimeout(total=420)
        connector = aiohttp.TCPConnector(
            limit=MAX_CONCURRENT_REQUESTS,
            family=socket.AF_INET,
        )

        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            teams_url = f"{MLB_API_BASE}/teams?sportId=1"
            teams_data = await fetch_json(session, sem, teams_url)
            teams = teams_data.get("teams", [])

            if not teams:
                raise RuntimeError("No teams returned from MLB API")

            all_players = []

            for idx, team in enumerate(teams, start=1):
                team_players = await process_team(
                    session=session,
                    sem=sem,
                    team=team,
                    season_year=season_year,
                    team_index=idx,
                    team_total=len(teams),
                )

                all_players.extend(team_players)
                all_players.sort(key=lambda p: (p["team"], p["name"]))

                with cache_lock:
                    cached_players = list(all_players)
                    load_progress = f"Loaded {len(all_players)} players so far..."

        with cache_lock:
            cached_players = all_players
            cache_loaded = True
            cache_loading = False
            last_updated = datetime.utcnow().isoformat() + "Z"
            last_load_error = None
            load_progress = f"Loaded {len(all_players)} players."

        logging.info("Refresh complete. Loaded %s players.", len(all_players))

    except Exception as e:
        with cache_lock:
            cache_loading = False
            last_load_error = str(e)
            load_progress = f"Load failed: {e}"

        logging.exception("MLB data refresh failed")


def loader_loop():
    while True:
        try:
            asyncio.run(load_all_players())
        except Exception as e:
            logging.exception("Loader loop failed: %s", e)

        time.sleep(REFRESH_SECONDS)


def ensure_loader_started():
    global loader_thread, load_progress

    with loader_lock:
        if loader_thread is None or not loader_thread.is_alive():
            with cache_lock:
                load_progress = "Starting MLB data refresh..."

            logging.info("Starting MLB loader thread...")
            loader_thread = threading.Thread(
                target=loader_loop,
                daemon=True,
            )
            loader_thread.start()


@app.route("/")
def index():
    ensure_loader_started()
    return HTML_PAGE


@app.route("/api/players")
@app.route("/api/player_stats")
def api_players():
    ensure_loader_started()

    team = request.args.get("team", "").strip()
    search = request.args.get("search", "").lower()
    selected = request.args.get("selected", "").strip()
    sort = request.args.get("sort", "name")
    dir_ = request.args.get("dir", "asc")

    with cache_lock:
        players = list(cached_players)
        loaded = cache_loaded
        loading = cache_loading
        error = last_load_error
        updated = last_updated
        progress = load_progress
        all_cached = list(cached_players)

    if team:
        players = [p for p in players if p["team"] == team]

    if search:
        players = [p for p in players if search in p["name"].lower()]

    if selected:
        selected_names = {
            name.strip()
            for name in selected.split("|")
            if name.strip()
        }
        players = [p for p in players if p["name"] in selected_names]

    def sort_key(p):
        return {
            "name": p["name"].lower(),
            "team": p["team"],
            "l5": p["l5_avg"] or 0,
            "l10": p["l10_avg"] or 0,
            "season": p["season_avg"] or 0,
            "ab": p["ab"],
        }.get(sort, p["name"].lower())

    players.sort(key=sort_key, reverse=(dir_ == "desc"))

    return jsonify({
        "loaded": loaded,
        "loading": loading,
        "last_load_error": error,
        "last_updated": updated,
        "load_progress": progress,
        "count": len(players),
        "players": players,
        "teams": sorted(set(p["team"] for p in all_cached)),
        "all_players": sorted(p["name"] for p in all_cached),
    })


@app.route("/healthz")
def healthz():
    ensure_loader_started()

    with cache_lock:
        return jsonify({
            "ok": True,
            "loaded": cache_loaded,
            "loading": cache_loading,
            "players_loaded": len(cached_players),
            "last_updated": last_updated,
            "last_load_error": last_load_error,
            "load_progress": load_progress,
            "loader_alive": loader_thread is not None and loader_thread.is_alive(),
        })


HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>MLB Slam Or Slump Dashboard</title>
<style>
body { font-family: Arial, sans-serif; padding: 20px; background:#181a1b; color:white; }
h1 { color:white; }
.controls { margin-bottom:20px; display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
select,input,button { padding:7px; }
table { width:100%; border-collapse:collapse; background:#222; }
th,td { padding:9px; text-align:center; border:1px solid #555; }
th { background:#2f6fa3; color:white; cursor:pointer; position:sticky; top:0; }
tr:nth-child(even){ background:#2a2a2a; }
tr:nth-child(odd){ background:#202020; }
.muted { color:#bbb; font-size:.9em; }
.error { color:#ffb3b3; font-size:.9em; }
#playerPicker { min-width:220px; height:36px; }
</style>
</head>
<body>
<h1>MLB Slam Or Slump Dashboard</h1>

<div class="controls">
<label>Team:
<select id="team"><option value="">All Teams</option></select>
</label>

<label>Search:
<input id="search" type="text" placeholder="Search player name">
</label>

<label>Player Picker:
<select id="playerPicker" multiple></select>
</label>

<button id="clearPlayers" type="button">Clear Player Picker</button>
<button id="applyBtn" type="button">Apply</button>
<span class="muted" id="statusText">Loading...</span>
</div>

<div class="error" id="errorText"></div>

<table>
<thead>
<tr>
<th data-sort="name">Player</th>
<th data-sort="team">Team</th>
<th data-sort="l5">L5 AVG</th>
<th data-sort="l10">L10 AVG</th>
<th data-sort="season">Total AVG</th>
<th data-sort="ab">AB</th>
</tr>
</thead>
<tbody id="playerRows"></tbody>
</table>

<script>
let currentSort = "name";
let currentDir = "asc";

function shade(diff) {
    const val = Math.min(Math.abs(diff) * 1000, 180);
    if (diff > 0) return `rgb(255, ${255 - val}, ${255 - val})`;
    if (diff < 0) return `rgb(${255 - val}, ${255 - val}, 255)`;
    return "transparent";
}

function selectedPlayers() {
    return Array.from(document.getElementById("playerPicker").selectedOptions)
        .map(o => o.value)
        .join("|");
}

function populateFilters(data) {
    const teamSelect = document.getElementById("team");
    const currentTeam = teamSelect.value;

    if (teamSelect.options.length <= 1 && data.teams.length) {
        data.teams.forEach(team => {
            const opt = document.createElement("option");
            opt.value = team;
            opt.textContent = team;
            teamSelect.appendChild(opt);
        });
        teamSelect.value = currentTeam;
    }

    const picker = document.getElementById("playerPicker");
    if (picker.options.length === 0 && data.all_players.length) {
        data.all_players.forEach(name => {
            const opt = document.createElement("option");
            opt.value = name;
            opt.textContent = name;
            picker.appendChild(opt);
        });
    }
}

function renderRows(data) {
    const tbody = document.getElementById("playerRows");
    tbody.innerHTML = "";

    data.players.forEach(player => {
        const row = document.createElement("tr");
        row.innerHTML = `
            <td>${player.name}</td>
            <td>${player.team}</td>
            <td style="background:${shade(player.diff_l5)}; color:#111;">${player.l5}</td>
            <td style="background:${shade(player.diff_l10)}; color:#111;">${player.l10}</td>
            <td>${player.season}</td>
            <td>${player.ab}</td>
        `;
        tbody.appendChild(row);
    });
}

function loadPlayers() {
    const statusText = document.getElementById("statusText");
    const errorText = document.getElementById("errorText");

    statusText.textContent = "Loading...";
    errorText.textContent = "";

    const params = new URLSearchParams({
        team: document.getElementById("team").value,
        search: document.getElementById("search").value,
        selected: selectedPlayers(),
        sort: currentSort,
        dir: currentDir
    });

    fetch(`/api/players?${params.toString()}`)
        .then(r => r.json())
        .then(data => {
            populateFilters(data);
            renderRows(data);

            if (data.last_load_error) {
                errorText.textContent = `Loader error: ${data.last_load_error}`;
            }

            if (!data.loaded) {
                statusText.textContent = `${data.load_progress || "Data loading"} | ${data.count} shown so far`;
                setTimeout(loadPlayers, 3000);
                return;
            }

            statusText.textContent = `${data.count} players shown` +
                (data.last_updated ? ` | Updated ${data.last_updated}` : "");
        })
        .catch(err => {
            console.error(err);
            statusText.textContent = "Error loading players";
        });
}

document.getElementById("applyBtn").addEventListener("click", loadPlayers);

document.getElementById("clearPlayers").addEventListener("click", () => {
    Array.from(document.getElementById("playerPicker").options).forEach(o => o.selected = false);
    loadPlayers();
});

document.getElementById("search").addEventListener("keydown", e => {
    if (e.key === "Enter") loadPlayers();
});

document.querySelectorAll("th[data-sort]").forEach(th => {
    th.addEventListener("click", () => {
        const sort = th.dataset.sort;
        if (currentSort === sort) {
            currentDir = currentDir === "asc" ? "desc" : "asc";
        } else {
            currentSort = sort;
            currentDir = "asc";
        }
        loadPlayers();
    });
});

loadPlayers();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
```
