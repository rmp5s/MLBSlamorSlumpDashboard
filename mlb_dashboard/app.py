import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request
import aiohttp

app = Flask(__name__)

logging.basicConfig(
    filename="dashboard.log",
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"
REFRESH_SECONDS = 6 * 60 * 60
LOOKBACK_DAYS = 90

cached_players = []
cache_lock = threading.Lock()


def format_avg(avg):
    if avg is None:
        return ".000"
    return ".%03d" % int(round(avg * 1000))


def calc_avg(hits, ab):
    return hits / ab if ab > 0 else None


async def fetch_json(session, url, params=None):
    try:
        async with session.get(url, params=params, timeout=30) as resp:
            if resp.status != 200:
                logging.warning(f"HTTP {resp.status} for {url}")
                return {}
            return await resp.json()
    except Exception as e:
        logging.warning(f"Fetch error for {url}: {e}")
        return {}


def avg_from_games(games):
    hits = sum(int(g.get("hits", 0)) for g in games)
    ab = sum(int(g.get("atBats", 0)) for g in games)
    return calc_avg(hits, ab)


async def fetch_season_stat(session, player_id, season_year):
    url = f"{MLB_API_BASE}/people/{player_id}/stats"
    params = {
        "stats": "season",
        "group": "hitting",
        "season": str(season_year)
    }

    data = await fetch_json(session, url, params)
    stats_list = data.get("stats", [])

    if not stats_list:
        return None

    splits = stats_list[0].get("splits", [])
    if not splits:
        return None

    return splits[0].get("stat", {})


async def get_recent_team_games(session, team_id):
    end = datetime.utcnow()
    start = end - timedelta(days=LOOKBACK_DAYS)

    url = f"{MLB_API_BASE}/schedule"
    params = {
        "sportId": 1,
        "teamId": team_id,
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate": end.strftime("%Y-%m-%d")
    }

    data = await fetch_json(session, url, params)

    games = []
    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            status = game.get("status", {}).get("detailedState", "")
            if status in ("Final", "Game Over", "Completed Early"):
                games.append({
                    "gamePk": game["gamePk"],
                    "gameDate": game.get("gameDate", "")
                })

    games.sort(key=lambda g: g["gameDate"], reverse=True)
    return games[:25]


async def fetch_boxscore(session, game_pk):
    url = f"{MLB_API_BASE}/game/{game_pk}/boxscore"
    return await fetch_json(session, url)


def get_player_batting_from_boxscore(boxscore, player_id):
    teams = boxscore.get("teams", {})
    all_players = {}

    home_players = teams.get("home", {}).get("players", {})
    away_players = teams.get("away", {}).get("players", {})

    all_players.update(home_players)
    all_players.update(away_players)

    player_key = f"ID{player_id}"
    player_data = all_players.get(player_key, {})
    batting = player_data.get("stats", {}).get("batting")

    if not batting:
        return None

    return {
        "hits": int(batting.get("hits", 0)),
        "atBats": int(batting.get("atBats", 0))
    }


async def process_team(session, team, season_year):
    team_id = team["id"]
    team_abbr = team["abbreviation"]
    team_name = team.get("name", team_abbr)

    logging.info(f"Loading team: {team_name}")

    roster_url = f"{MLB_API_BASE}/teams/{team_id}/roster"
    roster_data = await fetch_json(session, roster_url)
    roster = roster_data.get("roster", [])

    batters = [
        p for p in roster
        if p.get("position", {}).get("abbreviation") != "P"
    ]

    recent_games = await get_recent_team_games(session, team_id)
    game_pks = [g["gamePk"] for g in recent_games]

    boxscores = await asyncio.gather(
        *[fetch_boxscore(session, game_pk) for game_pk in game_pks]
    )

    season_tasks = [
        fetch_season_stat(session, p["person"]["id"], season_year)
        for p in batters
    ]
    season_stats_list = await asyncio.gather(*season_tasks)

    team_players = []

    for player, season_stat in zip(batters, season_stats_list):
        if not season_stat:
            continue

        person = player["person"]
        player_id = person["id"]
        name = person["fullName"]

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

        l5_games = recent_player_games[:5]
        l10_games = recent_player_games[:10]

        l5_avg = avg_from_games(l5_games)
        l10_avg = avg_from_games(l10_games)

        team_players.append({
            "name": name,
            "team": team_abbr,
            "season_avg": season_avg,
            "ab": season_ab,
            "l5_avg": l5_avg,
            "l10_avg": l10_avg,
            "l5": format_avg(l5_avg),
            "l10": format_avg(l10_avg),
            "season": format_avg(season_avg),
            "diff_l5": (l5_avg or 0) - (season_avg or 0),
            "diff_l10": (l10_avg or 0) - (season_avg or 0)
        })

    logging.info(f"{team_abbr}: loaded {len(team_players)} players")
    return team_players


async def load_all_players():
    logging.info("Starting MLB data refresh...")

    season_year = datetime.utcnow().year

    async with aiohttp.ClientSession() as session:
        teams_url = f"{MLB_API_BASE}/teams?sportId=1"
        teams_data = await fetch_json(session, teams_url)
        teams = teams_data.get("teams", [])

        all_team_results = await asyncio.gather(
            *[process_team(session, team, season_year) for team in teams]
        )

    players = []
    for team_players in all_team_results:
        players.extend(team_players)

    players.sort(key=lambda p: (p["team"], p["name"]))

    with cache_lock:
        global cached_players
        cached_players = players

    logging.info(f"Refresh complete. Loaded {len(players)} players.")


def background_refresh_loop():
    asyncio.run(load_all_players())

    while True:
        time.sleep(REFRESH_SECONDS)
        try:
            asyncio.run(load_all_players())
        except Exception as e:
            logging.exception(f"Background refresh failed: {e}")


@app.route("/")
def index():
    with cache_lock:
        teams = sorted(set(p["team"] for p in cached_players))

    return render_template("dashboard_shell.html", team_names=teams)


@app.route("/api/player_stats")
def api_player_stats():
    team = request.args.get("team", "").strip()
    search = request.args.get("search", "").lower()
    sort = request.args.get("sort", "name")
    dir_ = request.args.get("dir", "asc")

    with cache_lock:
        players = list(cached_players)

    if team:
        players = [p for p in players if p["team"] == team]

    if search:
        players = [p for p in players if search in p["name"].lower()]

    def sort_key(p):
        return {
            "name": p["name"].lower(),
            "team": p["team"],
            "l5": p["l5_avg"] or 0,
            "l10": p["l10_avg"] or 0,
            "season": p["season_avg"] or 0,
            "ab": p["ab"]
        }.get(sort, p["name"].lower())

    players.sort(key=sort_key, reverse=(dir_ == "desc"))
    return jsonify(players)


threading.Thread(target=background_refresh_loop, daemon=True).start()
