import asyncio
import csv
import io
import json
import logging
import math
import os
import threading
import time
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import aiohttp
from flask import Flask, Response, jsonify, render_template_string, request

APP_TITLE = "MLB Slam Or Slump Dashboard"
STAT_API_BASE = "https://statsapi.mlb.com/api/v1"

# Render environment variables you can override if needed.
SEASON = int(os.environ.get("MLB_SEASON", str(date.today().year)))
MIN_SEASON_AB = int(os.environ.get("MIN_SEASON_AB", "1"))
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "60"))
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", str(6 * 60 * 60)))
RETRY_SECONDS = int(os.environ.get("RETRY_SECONDS", "300"))
HTTP_TIMEOUT_SECONDS = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "30"))
BOX_SCORE_CONCURRENCY = int(os.environ.get("BOX_SCORE_CONCURRENCY", "12"))

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

logger = logging.getLogger("mlb_dashboard")
logger.setLevel(logging.INFO)
if not logger.handlers:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    try:
        file_handler = logging.FileHandler("/tmp/mlb_dashboard.log")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception:
        pass

cache_lock = threading.RLock()
thread_lock = threading.Lock()
wakeup_event = threading.Event()
refresh_thread = None

cache = {
    "players": [],
    "teams": [],
    "filter_options": {"teams": [], "leagues": [], "divisions": []},
    "loaded": False,
    "loading": False,
    "load_progress": "Not started",
    "players_loaded": 0,
    "boxscores_loaded": 0,
    "boxscores_total": 0,
    "last_updated": None,
    "last_load_error": None,
    "next_refresh_ts": 0,
    "refresh_count": 0,
    "season": SEASON,
}


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ts_to_iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def int_safe(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return default


def calc_avg(hits, at_bats):
    hits = int_safe(hits)
    at_bats = int_safe(at_bats)
    if at_bats <= 0:
        return None
    return hits / at_bats


def fmt_avg(value):
    if value is None:
        return "—"
    try:
        value = float(value)
    except Exception:
        return "—"
    if math.isnan(value) or value < 0:
        return "—"
    text = f"{value:.3f}"
    return text[1:] if 0 <= value < 1 else text


def set_progress(message, **updates):
    with cache_lock:
        cache["load_progress"] = message
        cache.update(updates)
    logger.info(message)


async def fetch_json(session, url, params=None, label=None, retries=2):
    last_error = None
    for attempt in range(retries + 1):
        try:
            async with session.get(url, params=params) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}: {text[:400]}")
                return json.loads(text)
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(0.8 * (attempt + 1))
            else:
                where = label or url
                raise RuntimeError(f"Failed fetching {where}: {last_error}") from last_error


async def load_all_data_async():
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    connector = aiohttp.TCPConnector(limit=max(BOX_SCORE_CONCURRENCY + 8, 20), ttl_dns_cache=300)

    today = date.today()
    if SEASON == today.year:
        end_date = today
    else:
        # Good enough for past-season final data.
        end_date = date(SEASON, 11, 15)
    start_date = max(date(SEASON, 3, 1), end_date - timedelta(days=LOOKBACK_DAYS))

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        set_progress(f"Fetching MLB teams for {SEASON}...")
        teams_json = await fetch_json(
            session,
            f"{STAT_API_BASE}/teams",
            params={"sportId": 1, "season": SEASON},
            label="teams",
        )
        team_lookup = parse_teams(teams_json)
        teams_payload = sorted(team_lookup.values(), key=lambda t: t["abbr"])

        set_progress(f"Fetching season hitting stats for {SEASON}...")
        stats_json = await fetch_json(
            session,
            f"{STAT_API_BASE}/stats",
            params={
                "stats": "season",
                "group": "hitting",
                "playerPool": "ALL",
                "season": SEASON,
                "sportIds": 1,
                "limit": 10000,
            },
            label="season hitting stats",
        )
        season_hitters = parse_season_hitters(stats_json, team_lookup)
        set_progress(
            f"Found {len(season_hitters)} hitters with at least {MIN_SEASON_AB} AB.",
            players_loaded=len(season_hitters),
        )

        set_progress(f"Fetching schedule from {start_date.isoformat()} to {end_date.isoformat()}...")
        schedule_json = await fetch_json(
            session,
            f"{STAT_API_BASE}/schedule",
            params={
                "sportId": 1,
                "season": SEASON,
                "gameType": "R",
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
            },
            label="schedule",
        )
        games = parse_completed_games(schedule_json)
        set_progress(
            f"Fetching {len(games)} completed game boxscores for L5/L10 math...",
            boxscores_total=len(games),
            boxscores_loaded=0,
        )

        boxscores = await fetch_boxscores(session, games)
        set_progress("Parsing recent player batting lines...")
        recent_by_player = parse_recent_batting_lines(boxscores, season_hitters)

        set_progress("Building dashboard rows...")
        players = build_player_rows(season_hitters, recent_by_player, team_lookup)
        options = build_filter_options(players)
        set_progress(f"Loaded {len(players)} player rows.", players_loaded=len(players))
        return players, teams_payload, options


def parse_teams(teams_json):
    lookup = {}
    for team in teams_json.get("teams", []):
        team_id = int_safe(team.get("id"), None)
        if not team_id:
            continue
        lookup[team_id] = {
            "id": team_id,
            "name": team.get("name") or team.get("teamName") or str(team_id),
            "abbr": team.get("abbreviation") or team.get("fileCode") or team.get("teamName") or str(team_id),
            "league": (team.get("league") or {}).get("name") or "Unknown League",
            "division": (team.get("division") or {}).get("name") or "Unknown Division",
        }
    return lookup


def parse_season_hitters(stats_json, team_lookup):
    by_player = {}
    splits = []
    for block in stats_json.get("stats", []):
        splits.extend(block.get("splits", []))

    for split in splits:
        player = split.get("player") or {}
        stat = split.get("stat") or {}
        team = split.get("team") or {}

        player_id = int_safe(player.get("id"), None)
        if not player_id:
            continue

        at_bats = int_safe(stat.get("atBats"))
        hits = int_safe(stat.get("hits"))
        if at_bats <= 0:
            continue

        team_id = int_safe(team.get("id"), None)
        if team_id and team_id not in team_lookup:
            continue

        row = by_player.setdefault(
            player_id,
            {
                "id": player_id,
                "player": player.get("fullName") or player.get("name") or str(player_id),
                "season_hits": 0,
                "season_ab": 0,
                "team_abs": defaultdict(int),
            },
        )
        row["season_hits"] += hits
        row["season_ab"] += at_bats
        if team_id:
            row["team_abs"][team_id] += at_bats

    cleaned = {}
    for player_id, row in by_player.items():
        if row["season_ab"] < MIN_SEASON_AB:
            continue
        row["stat_team_id"] = max(row["team_abs"].items(), key=lambda kv: kv[1])[0] if row["team_abs"] else None
        row["season_avg"] = calc_avg(row["season_hits"], row["season_ab"])
        cleaned[player_id] = row
    return cleaned


def parse_completed_games(schedule_json):
    games = []
    seen = set()
    for day in schedule_json.get("dates", []):
        for game in day.get("games", []):
            game_pk = game.get("gamePk")
            if not game_pk or game_pk in seen:
                continue

            status = game.get("status") or {}
            abstract_state = status.get("abstractGameState")
            coded_state = status.get("codedGameState")
            detailed_state = status.get("detailedState") or ""
            is_final = (
                abstract_state == "Final"
                or coded_state in {"F", "O"}
                or detailed_state.lower() in {"final", "completed early", "game over"}
            )
            if not is_final:
                continue

            game_date = game.get("gameDate") or day.get("date")
            try:
                sort_ts = datetime.fromisoformat(game_date.replace("Z", "+00:00")).timestamp()
            except Exception:
                try:
                    sort_ts = datetime.fromisoformat(day.get("date")).timestamp()
                except Exception:
                    sort_ts = 0

            games.append({"gamePk": game_pk, "gameDate": game_date, "sort_ts": sort_ts})
            seen.add(game_pk)

    return sorted(games, key=lambda g: g["sort_ts"], reverse=True)


async def fetch_boxscores(session, games):
    sem = asyncio.Semaphore(max(1, BOX_SCORE_CONCURRENCY))
    results = []
    loaded_count = 0
    total = len(games)

    async def fetch_one(game):
        nonlocal loaded_count
        async with sem:
            try:
                data = await fetch_json(
                    session,
                    f"{STAT_API_BASE}/game/{game['gamePk']}/boxscore",
                    label=f"boxscore {game['gamePk']}",
                    retries=2,
                )
                data["_gamePk"] = game["gamePk"]
                data["_gameDate"] = game["gameDate"]
                data["_sort_ts"] = game["sort_ts"]
                return data
            except Exception as exc:
                logger.warning("Skipping boxscore %s: %s", game.get("gamePk"), exc)
                return None
            finally:
                loaded_count += 1
                if loaded_count == total or loaded_count % 25 == 0:
                    set_progress(
                        f"Fetched {loaded_count}/{total} boxscores...",
                        boxscores_loaded=loaded_count,
                        boxscores_total=total,
                    )

    if not games:
        return []
    gathered = await asyncio.gather(*(fetch_one(game) for game in games))
    results.extend([item for item in gathered if item])
    return results


def parse_recent_batting_lines(boxscores, season_hitters):
    wanted_ids = set(season_hitters.keys())
    recent_by_player = defaultdict(list)

    for box in boxscores:
        teams = box.get("teams") or {}
        for side_name in ("away", "home"):
            side = teams.get(side_name) or {}
            team_obj = side.get("team") or {}
            team_id = int_safe(team_obj.get("id"), None)
            players = side.get("players") or {}

            for player_blob in players.values():
                person = player_blob.get("person") or {}
                player_id = int_safe(person.get("id"), None)
                if not player_id or player_id not in wanted_ids:
                    continue

                batting = (player_blob.get("stats") or {}).get("batting") or {}
                if not batting:
                    continue

                at_bats = int_safe(batting.get("atBats"))
                hits = int_safe(batting.get("hits"))
                plate_appearances = int_safe(batting.get("plateAppearances"))
                games_played = int_safe(batting.get("gamesPlayed"))

                # Count real batting appearances. Games with 0 AB can still be counted if MLB
                # records a PA, but they contribute 0 AB to AVG.
                if at_bats <= 0 and hits <= 0 and plate_appearances <= 0 and games_played <= 0:
                    continue

                recent_by_player[player_id].append(
                    {
                        "game_pk": box.get("_gamePk"),
                        "game_date": box.get("_gameDate"),
                        "sort_ts": box.get("_sort_ts") or 0,
                        "team_id": team_id,
                        "hits": hits,
                        "ab": at_bats,
                    }
                )

    for player_id in recent_by_player:
        recent_by_player[player_id].sort(key=lambda line: (line["sort_ts"], line["game_pk"] or 0), reverse=True)
    return recent_by_player


def summarize_recent(lines, n):
    picked = lines[:n]
    hits = sum(int_safe(line.get("hits")) for line in picked)
    at_bats = sum(int_safe(line.get("ab")) for line in picked)
    return {
        "games": len(picked),
        "hits": hits,
        "ab": at_bats,
        "avg": calc_avg(hits, at_bats),
    }


def build_player_rows(season_hitters, recent_by_player, team_lookup):
    rows = []
    for player_id, hitter in season_hitters.items():
        recent_lines = recent_by_player.get(player_id, [])
        l5 = summarize_recent(recent_lines, 5)
        l10 = summarize_recent(recent_lines, 10)

        team_id = hitter.get("stat_team_id")
        if recent_lines and recent_lines[0].get("team_id") in team_lookup:
            team_id = recent_lines[0].get("team_id")
        team = team_lookup.get(team_id) or {
            "id": team_id or 0,
            "name": "Unknown Team",
            "abbr": "UNK",
            "league": "Unknown League",
            "division": "Unknown Division",
        }

        rows.append(
            {
                "id": player_id,
                "player": hitter["player"],
                "team_id": team["id"],
                "team": team["abbr"],
                "team_name": team["name"],
                "league": team["league"],
                "division": team["division"],
                "l5_avg": l5["avg"],
                "l5_avg_display": fmt_avg(l5["avg"]),
                "l5_hits": l5["hits"],
                "l5_ab": l5["ab"],
                "l5_games": l5["games"],
                "l10_avg": l10["avg"],
                "l10_avg_display": fmt_avg(l10["avg"]),
                "l10_hits": l10["hits"],
                "l10_ab": l10["ab"],
                "l10_games": l10["games"],
                "season_avg": hitter["season_avg"],
                "season_avg_display": fmt_avg(hitter["season_avg"]),
                "hits": hitter["season_hits"],
                "ab": hitter["season_ab"],
            }
        )

    rows.sort(key=lambda r: (r["team"], r["player"]))
    return rows


def build_filter_options(players):
    teams = {}
    leagues = set()
    divisions = set()
    for player in players:
        teams[player["team_id"]] = {
            "id": player["team_id"],
            "abbr": player["team"],
            "name": player["team_name"],
        }
        leagues.add(player["league"])
        divisions.add(player["division"])

    return {
        "teams": sorted(teams.values(), key=lambda t: t["abbr"]),
        "leagues": sorted(leagues),
        "divisions": sorted(divisions),
    }


def refresh_data_sync(manual=False):
    with cache_lock:
        if cache["loading"]:
            return
        cache["loading"] = True
        cache["load_progress"] = "Starting MLB data refresh..."
        cache["last_load_error"] = None
        cache["boxscores_loaded"] = 0
        cache["boxscores_total"] = 0

    logger.info("Starting MLB data refresh...")
    try:
        players, teams, options = asyncio.run(load_all_data_async())
        now_ts = time.time()
        with cache_lock:
            cache.update(
                {
                    "players": players,
                    "teams": teams,
                    "filter_options": options,
                    "loaded": True,
                    "loading": False,
                    "load_progress": f"Ready. Loaded {len(players)} players.",
                    "players_loaded": len(players),
                    "last_updated": utc_now_iso(),
                    "last_load_error": None,
                    "next_refresh_ts": now_ts + REFRESH_SECONDS,
                    "refresh_count": cache.get("refresh_count", 0) + 1,
                }
            )
        logger.info("MLB data refresh complete. Loaded %s players.", len(players))
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        logger.error("MLB data refresh failed: %s\n%s", error_text, traceback.format_exc())
        with cache_lock:
            cache.update(
                {
                    "loading": False,
                    "load_progress": "Load failed. Will retry.",
                    "last_load_error": error_text,
                    "next_refresh_ts": time.time() + RETRY_SECONDS,
                }
            )


def refresher_loop():
    logger.info("Background refresher thread started.")
    while True:
        try:
            with cache_lock:
                loaded = cache["loaded"]
                loading = cache["loading"]
                next_refresh_ts = cache.get("next_refresh_ts") or 0
            now = time.time()

            if not loading and (not loaded or wakeup_event.is_set() or now >= next_refresh_ts):
                wakeup_event.clear()
                refresh_data_sync()
                continue

            sleep_for = 60
            if next_refresh_ts:
                sleep_for = max(5, min(60, int(next_refresh_ts - now)))
            wakeup_event.wait(timeout=sleep_for)
        except Exception:
            logger.error("Background refresher loop crashed but will continue.\n%s", traceback.format_exc())
            time.sleep(10)


def ensure_refresher_started():
    global refresh_thread
    with thread_lock:
        if refresh_thread and refresh_thread.is_alive():
            return
        refresh_thread = threading.Thread(target=refresher_loop, name="mlb-data-refresher", daemon=True)
        refresh_thread.start()


@app.before_request
def before_any_request():
    ensure_refresher_started()


def current_status():
    ensure_refresher_started()
    with cache_lock:
        status = {
            "ok": True,
            "season": cache["season"],
            "loaded": cache["loaded"],
            "loading": cache["loading"],
            "load_progress": cache["load_progress"],
            "players_loaded": cache["players_loaded"],
            "boxscores_loaded": cache["boxscores_loaded"],
            "boxscores_total": cache["boxscores_total"],
            "last_updated": cache["last_updated"],
            "last_load_error": cache["last_load_error"],
            "next_refresh": ts_to_iso(cache.get("next_refresh_ts")),
            "refresh_count": cache.get("refresh_count", 0),
            "background_refresher_alive": bool(refresh_thread and refresh_thread.is_alive()),
        }
    return status


def get_filtered_players(args):
    with cache_lock:
        players = list(cache["players"])
        loaded = cache["loaded"]
        loading = cache["loading"]

    team = (args.get("team") or "").strip()
    league = (args.get("league") or "").strip()
    division = (args.get("division") or "").strip()
    search = (args.get("search") or "").strip().lower()
    player_ids_raw = (args.get("player_ids") or "").strip()

    selected_ids = set()
    if player_ids_raw:
        for token in player_ids_raw.split(","):
            token = token.strip()
            if token.isdigit():
                selected_ids.add(int(token))

    if team:
        players = [p for p in players if str(p.get("team_id")) == team]
    if league:
        players = [p for p in players if p.get("league") == league]
    if division:
        players = [p for p in players if p.get("division") == division]
    if search:
        players = [
            p
            for p in players
            if search in (p.get("player") or "").lower()
            or search in (p.get("team") or "").lower()
            or search in (p.get("team_name") or "").lower()
        ]
    if selected_ids:
        players = [p for p in players if int_safe(p.get("id")) in selected_ids]

    sort = (args.get("sort") or "team").strip().lower()
    direction = (args.get("dir") or "asc").strip().lower()
    reverse = direction == "desc"

    def num_key(field, missing=-9999):
        def inner(player):
            value = player.get(field)
            if value is None:
                return missing
            try:
                return float(value)
            except Exception:
                return missing
        return inner

    sorters = {
        "player": lambda p: (p.get("player") or "").lower(),
        "team": lambda p: ((p.get("team") or "").lower(), (p.get("player") or "").lower()),
        "l5": num_key("l5_avg"),
        "l10": num_key("l10_avg"),
        "season": num_key("season_avg"),
        "ab": num_key("ab"),
        "l5ab": num_key("l5_ab"),
        "l10ab": num_key("l10_ab"),
    }
    players.sort(key=sorters.get(sort, sorters["team"]), reverse=reverse)
    return players, loaded, loading


@app.get("/")
def index():
    return render_template_string(INDEX_HTML, title=APP_TITLE, season=SEASON)


@app.get("/healthz")
def healthz():
    return jsonify(current_status())


@app.get("/api/status")
def api_status():
    return jsonify(current_status())


@app.post("/api/refresh")
def api_refresh():
    with cache_lock:
        cache["next_refresh_ts"] = 0
        if not cache["loading"]:
            cache["load_progress"] = "Manual refresh queued..."
    wakeup_event.set()
    return jsonify(current_status())


@app.get("/api/filter_options")
def api_filter_options():
    with cache_lock:
        return jsonify({"loaded": cache["loaded"], "loading": cache["loading"], "options": cache["filter_options"]})


@app.get("/api/player_options")
def api_player_options():
    with cache_lock:
        players = list(cache["players"])
    options = [
        {"id": p["id"], "name": p["player"], "team": p["team"], "label": f"{p['player']} — {p['team']}"}
        for p in sorted(players, key=lambda x: (x.get("player") or "").lower())
    ]
    return jsonify({"players": options})


@app.get("/api/player_stats")
def api_player_stats():
    players, loaded, loading = get_filtered_players(request.args)
    return jsonify({"loaded": loaded, "loading": loading, "count": len(players), "players": players, "status": current_status()})


@app.get("/export.csv")
def export_csv():
    players, loaded, loading = get_filtered_players(request.args)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Player",
        "Team",
        "League",
        "Division",
        "L5 AVG",
        "L5 H",
        "L5 AB",
        "L5 Games",
        "L10 AVG",
        "L10 H",
        "L10 AB",
        "L10 Games",
        "Season AVG",
        "Season H",
        "Season AB",
    ])
    for p in players:
        writer.writerow([
            p.get("player"),
            p.get("team"),
            p.get("league"),
            p.get("division"),
            p.get("l5_avg_display"),
            p.get("l5_hits"),
            p.get("l5_ab"),
            p.get("l5_games"),
            p.get("l10_avg_display"),
            p.get("l10_hits"),
            p.get("l10_ab"),
            p.get("l10_games"),
            p.get("season_avg_display"),
            p.get("hits"),
            p.get("ab"),
        ])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=mlb_slam_or_slump_{stamp}.csv"},
    )


INDEX_HTML = r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }}</title>
  <style>
    :root {
      --bg: #181a1b;
      --panel: #202324;
      --panel2: #25292b;
      --text: #f2f2f2;
      --muted: #a9b0b4;
      --border: #3a3f42;
      --accent: #f4c542;
      --good: #75d36b;
      --bad: #ff7474;
      --link: #9ecbff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.35;
    }
    a { color: var(--link); }
    .wrap { max-width: 1450px; margin: 0 auto; padding: 18px; }
    .topbar { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; flex-wrap: wrap; margin-bottom: 14px; }
    h1 { margin: 0 0 6px; font-size: clamp(1.6rem, 2.8vw, 2.5rem); letter-spacing: .2px; }
    .sub { color: var(--muted); max-width: 860px; }
    .status-card { min-width: 310px; background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 12px; box-shadow: 0 10px 22px rgba(0,0,0,.16); }
    .status-line { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
    .dot { width: 10px; height: 10px; border-radius: 50%; background: #8c8c8c; display: inline-block; }
    .dot.ok { background: var(--good); }
    .dot.loading { background: var(--accent); animation: pulse 1.2s infinite; }
    .dot.err { background: var(--bad); }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
    .small { color: var(--muted); font-size: .9rem; }
    .error { color: #ff9c9c; margin-top: 6px; word-break: break-word; }
    .controls { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 12px; display: grid; grid-template-columns: repeat(12, 1fr); gap: 10px; align-items: end; margin-bottom: 14px; }
    .field { display: flex; flex-direction: column; gap: 5px; }
    .field label { color: var(--muted); font-size: .82rem; }
    .span2 { grid-column: span 2; }
    .span3 { grid-column: span 3; }
    .span4 { grid-column: span 4; }
    .span5 { grid-column: span 5; }
    .span12 { grid-column: span 12; }
    input, select, button { width: 100%; border-radius: 9px; border: 1px solid var(--border); background: #111314; color: var(--text); padding: 9px 10px; font: inherit; }
    button { cursor: pointer; background: var(--panel2); transition: transform .04s ease, background .12s ease; }
    button:hover { background: #303537; }
    button:active { transform: translateY(1px); }
    .button-row { display: flex; gap: 8px; flex-wrap: wrap; }
    .button-row button { width: auto; }
    .picker-row { display: flex; gap: 8px; align-items: center; }
    .picker-row input { flex: 1; min-width: 220px; }
    .picker-row button { width: auto; min-width: 84px; }
    .chips { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 8px; min-height: 28px; }
    .chip { display: inline-flex; align-items: center; gap: 7px; border: 1px solid var(--border); background: #111314; color: var(--text); padding: 5px 8px; border-radius: 999px; font-size: .9rem; }
    .chip button { border: 0; background: transparent; color: var(--muted); width: auto; padding: 0 2px; line-height: 1; }
    .table-card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
    .table-meta { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; padding: 10px 12px; border-bottom: 1px solid var(--border); color: var(--muted); }
    .table-wrap { overflow-x: auto; max-height: calc(100vh - 320px); }
    table { width: 100%; border-collapse: collapse; min-width: 980px; }
    th, td { padding: 9px 10px; border-bottom: 1px solid #303436; text-align: left; white-space: nowrap; }
    th { position: sticky; top: 0; z-index: 1; background: #363c40; color: #ffffff; cursor: pointer; user-select: none; font-weight: 700; font-size: .92rem; }
    th:hover { background: #444b50; }
    td.num { text-align: right; font-variant-numeric: tabular-nums; }
    td.player { font-weight: 650; }
    tr:hover td { background-color: rgba(255,255,255,.035); }
    .avg-cell { font-weight: 750; text-align: right; font-variant-numeric: tabular-nums; border-left: 1px solid rgba(255,255,255,.04); }
    .empty { padding: 36px 12px; text-align: center; color: var(--muted); }
    .legend { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .swatch { display:inline-block; width: 14px; height: 14px; border-radius: 4px; vertical-align: -2px; margin-right: 4px; }
    .swatch.red { background: rgba(230,80,80,.65); }
    .swatch.blue { background: rgba(80,150,255,.65); }
    @media (max-width: 950px) {
      .controls { grid-template-columns: 1fr; }
      .span2, .span3, .span4, .span5, .span12 { grid-column: span 1; }
      .status-card { min-width: 100%; }
      .table-wrap { max-height: none; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <h1>{{ title }}</h1>
        <div class="sub">
          Season {{ season }} hitter dashboard. L5/L10 are calculated from each player's most recent games in completed regular-season boxscores.
          Red means recent AVG is above season AVG. Blue means recent AVG is below season AVG.
        </div>
      </div>
      <div class="status-card">
        <div class="status-line"><span id="status-dot" class="dot"></span><strong id="status-main">Starting...</strong></div>
        <div id="status-detail" class="small">Loading status...</div>
        <div id="status-error" class="error" style="display:none;"></div>
      </div>
    </div>

    <div class="controls">
      <div class="field span3">
        <label for="search">Search player/team</label>
        <input id="search" type="search" placeholder="carroll, judge, dbacks...">
      </div>
      <div class="field span2">
        <label for="team">Team</label>
        <select id="team"><option value="">All teams</option></select>
      </div>
      <div class="field span3">
        <label for="league">League</label>
        <select id="league"><option value="">All leagues</option></select>
      </div>
      <div class="field span3">
        <label for="division">Division</label>
        <select id="division"><option value="">All divisions</option></select>
      </div>
      <div class="field span1">
        <label>&nbsp;</label>
        <button id="clear-btn" type="button">Clear</button>
      </div>

      <div class="field span12">
        <label for="player-picker">Small player picker, max 10</label>
        <div class="picker-row">
          <input id="player-picker" list="player-list" placeholder="Type a player name, then Add">
          <datalist id="player-list"></datalist>
          <button id="add-player-btn" type="button">Add</button>
          <button id="clear-players-btn" type="button">Clear players</button>
        </div>
        <div id="chips" class="chips"></div>
      </div>

      <div class="span12 button-row">
        <button id="refresh-btn" type="button">Refresh data now</button>
        <button id="export-btn" type="button">Export CSV</button>
      </div>
    </div>

    <div class="table-card">
      <div class="table-meta">
        <div id="result-count">0 rows</div>
        <div class="legend">
          <span><span class="swatch red"></span>Recent above season</span>
          <span><span class="swatch blue"></span>Recent below season</span>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th data-sort="player">Player</th>
              <th data-sort="team">Team</th>
              <th data-sort="l5">L5 AVG</th>
              <th data-sort="l5ab">L5 AB</th>
              <th data-sort="l10">L10 AVG</th>
              <th data-sort="l10ab">L10 AB</th>
              <th data-sort="season">Season AVG</th>
              <th data-sort="ab">AB</th>
              <th>League</th>
              <th>Division</th>
            </tr>
          </thead>
          <tbody id="tbody">
            <tr><td class="empty" colspan="10">Data loading...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

<script>
const state = {
  sort: 'team',
  dir: 'asc',
  selectedPlayers: [],
  playerOptions: [],
  debounceTimer: null,
};

const el = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  }[ch]));
}

function formatTs(value) {
  if (!value) return 'never';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString();
}

async function getJson(url, options) {
  const res = await fetch(url, options || {});
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return await res.json();
}

function setStatus(status) {
  const dot = el('status-dot');
  const main = el('status-main');
  const detail = el('status-detail');
  const error = el('status-error');

  dot.className = 'dot';
  if (status.last_load_error) dot.classList.add('err');
  else if (status.loading) dot.classList.add('loading');
  else if (status.loaded) dot.classList.add('ok');

  main.textContent = status.loading ? 'Loading MLB data...' : (status.loaded ? 'Ready' : 'Not loaded yet');

  let progress = status.load_progress || '';
  if (status.boxscores_total && status.loading) {
    progress += ` (${status.boxscores_loaded}/${status.boxscores_total} boxscores)`;
  }
  detail.innerHTML = `
    <div>${escapeHtml(progress)}</div>
    <div>Players: ${status.players_loaded || 0}</div>
    <div>Last updated: ${escapeHtml(formatTs(status.last_updated))}</div>
    <div>Next refresh: ${escapeHtml(formatTs(status.next_refresh))}</div>
  `;

  if (status.last_load_error) {
    error.style.display = '';
    error.textContent = status.last_load_error;
  } else {
    error.style.display = 'none';
    error.textContent = '';
  }
}

async function refreshStatus() {
  try {
    const status = await getJson('/api/status');
    setStatus(status);
    return status;
  } catch (err) {
    el('status-main').textContent = 'Status error';
    el('status-detail').textContent = err.message;
    return null;
  }
}

async function loadFilterOptions() {
  const data = await getJson('/api/filter_options');
  const options = data.options || {teams: [], leagues: [], divisions: []};

  const team = el('team');
  const league = el('league');
  const division = el('division');

  const oldTeam = team.value;
  const oldLeague = league.value;
  const oldDivision = division.value;

  team.innerHTML = '<option value="">All teams</option>' + options.teams.map(t =>
    `<option value="${escapeHtml(t.id)}">${escapeHtml(t.abbr)} — ${escapeHtml(t.name)}</option>`
  ).join('');
  league.innerHTML = '<option value="">All leagues</option>' + options.leagues.map(v =>
    `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`
  ).join('');
  division.innerHTML = '<option value="">All divisions</option>' + options.divisions.map(v =>
    `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`
  ).join('');

  team.value = oldTeam;
  league.value = oldLeague;
  division.value = oldDivision;
}

async function loadPlayerOptions() {
  const data = await getJson('/api/player_options');
  state.playerOptions = data.players || [];
  const list = el('player-list');
  list.innerHTML = state.playerOptions.map(p =>
    `<option value="${escapeHtml(p.label)}"></option>`
  ).join('');
}

function queryParams() {
  const params = new URLSearchParams();
  params.set('sort', state.sort);
  params.set('dir', state.dir);
  const search = el('search').value.trim();
  const team = el('team').value;
  const league = el('league').value;
  const division = el('division').value;
  if (search) params.set('search', search);
  if (team) params.set('team', team);
  if (league) params.set('league', league);
  if (division) params.set('division', division);
  if (state.selectedPlayers.length) params.set('player_ids', state.selectedPlayers.map(p => p.id).join(','));
  return params;
}

function avgBackground(recent, season) {
  if (recent === null || recent === undefined || season === null || season === undefined) return '';
  const diff = recent - season;
  if (!Number.isFinite(diff) || Math.abs(diff) < 0.001) return '';

  // Scale roughly: .000 no color, .150+ strong color.
  const strength = Math.min(0.82, Math.max(0.12, Math.abs(diff) / 0.150 * 0.82));
  if (diff > 0) return `background-color: rgba(230, 80, 80, ${strength});`;
  return `background-color: rgba(80, 150, 255, ${strength});`;
}

function renderRows(players, loaded, loading) {
  const tbody = el('tbody');
  el('result-count').textContent = `${players.length} row${players.length === 1 ? '' : 's'}`;

  if (!players.length) {
    const msg = loading ? 'Data is still loading. This page will keep checking.' : (loaded ? 'No players match the current filters.' : 'Waiting for the first data load.');
    tbody.innerHTML = `<tr><td class="empty" colspan="10">${escapeHtml(msg)}</td></tr>`;
    return;
  }

  tbody.innerHTML = players.map(p => {
    const l5Style = avgBackground(p.l5_avg, p.season_avg);
    const l10Style = avgBackground(p.l10_avg, p.season_avg);
    return `
      <tr>
        <td class="player">${escapeHtml(p.player)}</td>
        <td title="${escapeHtml(p.team_name)}">${escapeHtml(p.team)}</td>
        <td class="avg-cell" style="${l5Style}" title="${p.l5_hits}/${p.l5_ab} over ${p.l5_games} games">${escapeHtml(p.l5_avg_display)}</td>
        <td class="num">${escapeHtml(p.l5_ab)}</td>
        <td class="avg-cell" style="${l10Style}" title="${p.l10_hits}/${p.l10_ab} over ${p.l10_games} games">${escapeHtml(p.l10_avg_display)}</td>
        <td class="num">${escapeHtml(p.l10_ab)}</td>
        <td class="num">${escapeHtml(p.season_avg_display)}</td>
        <td class="num">${escapeHtml(p.ab)}</td>
        <td>${escapeHtml(p.league)}</td>
        <td>${escapeHtml(p.division)}</td>
      </tr>`;
  }).join('');
}

async function loadTable() {
  try {
    const data = await getJson('/api/player_stats?' + queryParams().toString());
    setStatus(data.status);
    renderRows(data.players || [], data.loaded, data.loading);
    return data;
  } catch (err) {
    el('tbody').innerHTML = `<tr><td class="empty" colspan="10">Table load error: ${escapeHtml(err.message)}</td></tr>`;
    return null;
  }
}

function scheduleLoadTable() {
  clearTimeout(state.debounceTimer);
  state.debounceTimer = setTimeout(loadTable, 220);
}

function renderChips() {
  const chips = el('chips');
  if (!state.selectedPlayers.length) {
    chips.innerHTML = '<span class="small">No specific players selected.</span>';
    return;
  }
  chips.innerHTML = state.selectedPlayers.map(p => `
    <span class="chip">
      ${escapeHtml(p.label)}
      <button type="button" data-remove-player="${escapeHtml(p.id)}" title="Remove">×</button>
    </span>
  `).join('');

  chips.querySelectorAll('button[data-remove-player]').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = Number(btn.getAttribute('data-remove-player'));
      state.selectedPlayers = state.selectedPlayers.filter(p => p.id !== id);
      renderChips();
      loadTable();
    });
  });
}

function addSelectedPlayer() {
  const input = el('player-picker');
  const typed = input.value.trim().toLowerCase();
  if (!typed) return;

  let found = state.playerOptions.find(p => p.label.toLowerCase() === typed);
  if (!found) found = state.playerOptions.find(p => p.name.toLowerCase() === typed);
  if (!found) found = state.playerOptions.find(p => p.label.toLowerCase().includes(typed));
  if (!found) return;

  if (state.selectedPlayers.some(p => p.id === found.id)) {
    input.value = '';
    return;
  }
  if (state.selectedPlayers.length >= 10) {
    alert('Max 10 players. Remove one first.');
    return;
  }
  state.selectedPlayers.push(found);
  input.value = '';
  renderChips();
  loadTable();
}

function attachEvents() {
  ['search', 'team', 'league', 'division'].forEach(id => {
    el(id).addEventListener(id === 'search' ? 'input' : 'change', scheduleLoadTable);
  });

  document.querySelectorAll('th[data-sort]').forEach(th => {
    th.addEventListener('click', () => {
      const sort = th.getAttribute('data-sort');
      if (state.sort === sort) state.dir = state.dir === 'asc' ? 'desc' : 'asc';
      else { state.sort = sort; state.dir = (sort === 'player' || sort === 'team') ? 'asc' : 'desc'; }
      loadTable();
    });
  });

  el('clear-btn').addEventListener('click', () => {
    el('search').value = '';
    el('team').value = '';
    el('league').value = '';
    el('division').value = '';
    loadTable();
  });

  el('add-player-btn').addEventListener('click', addSelectedPlayer);
  el('player-picker').addEventListener('keydown', (evt) => {
    if (evt.key === 'Enter') {
      evt.preventDefault();
      addSelectedPlayer();
    }
  });
  el('clear-players-btn').addEventListener('click', () => {
    state.selectedPlayers = [];
    renderChips();
    loadTable();
  });

  el('refresh-btn').addEventListener('click', async () => {
    el('refresh-btn').disabled = true;
    try {
      const status = await getJson('/api/refresh', { method: 'POST' });
      setStatus(status);
    } catch (err) {
      alert('Refresh request failed: ' + err.message);
    } finally {
      setTimeout(() => { el('refresh-btn').disabled = false; }, 2500);
    }
  });

  el('export-btn').addEventListener('click', () => {
    window.location.href = '/export.csv?' + queryParams().toString();
  });
}

async function boot() {
  attachEvents();
  renderChips();
  await refreshStatus();
  await loadFilterOptions().catch(() => {});
  await loadPlayerOptions().catch(() => {});
  await loadTable();

  setInterval(async () => {
    const status = await refreshStatus();
    if (status && status.loaded) {
      loadFilterOptions().catch(() => {});
      loadPlayerOptions().catch(() => {});
    }
    if (status && (status.loading || !status.loaded)) {
      loadTable();
    }
  }, 3000);
}

boot();
</script>
</body>
</html>
'''


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
