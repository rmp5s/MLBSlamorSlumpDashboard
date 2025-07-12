from flask import Flask, render_template, request
import requests

app = Flask(__name__)

TEAM_NAME_MAP = {}

def get_team_ids():
    url = "https://statsapi.mlb.com/api/v1/teams?sportId=1"
    return [team['id'] for team in requests.get(url).json()['teams']]

def get_starting_lineup(team_id):
    roster = requests.get(f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster").json()['roster']
    return [p['person']['id'] for p in roster if p['position']['code'] != "1"]

def get_player_name(player_id):
    return requests.get(f"https://statsapi.mlb.com/api/v1/people/{player_id}").json()['people'][0]['fullName']

def get_team_name(team_id):
    if team_id in TEAM_NAME_MAP:
        return TEAM_NAME_MAP[team_id]
    name = requests.get(f"https://statsapi.mlb.com/api/v1/teams/{team_id}").json()['teams'][0]['name']
    TEAM_NAME_MAP[team_id] = name
    return name

def get_batting_averages(player_id):
    try:
        gl = requests.get(f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats?stats=gameLog&group=hitting&season=2025").json()['stats'][0]['splits']
        def calc(games):
            hits = sum(int(g['stat']['hits']) for g in games)
            atb = sum(int(g['stat']['atBats']) for g in games)
            return round(hits/atb, 3) if atb else 0.0, atb
        l5, ab5 = calc(gl[:5])
        l10, ab10 = calc(gl[:10])
        ab = ab10
        total = float(requests.get(f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats?stats=season&group=hitting&season=2025").json()['stats'][0]['splits'][0]['stat']['avg'])
        return l5, l10, total, ab
    except:
        return None

@app.route("/")
def dashboard():
    team_ids = get_team_ids()
    selected_team = request.args.get("team", "")
    search = request.args.get("search", "").lower()
    sort_key = request.args.get("sort", "name")
    sort_dir = request.args.get("dir", "asc")
    seen = set()
    players = []
    teams = []

    for tid in team_ids:
        team = get_team_name(tid)
        teams.append(team)
        if selected_team and team != selected_team:
            continue
        for pid in get_starting_lineup(tid):
            if pid in seen:
                continue
            seen.add(pid)
            name = get_player_name(pid)
            if search and search not in name.lower():
                continue
            avgs = get_batting_averages(pid)
            if not avgs:
                continue
            l5, l10, season, ab = avgs
            players.append({
                "name": name,
                "team": team,
                "l5": f"{l5:.3f}",
                "l10": f"{l10:.3f}",
                "season": f"{season:.3f}",
                "ab": ab,
                "diff_l5": round(l5 - season, 3),
                "diff_l10": round(l10 - season, 3)
            })

    reverse = sort_dir == "desc"
    players.sort(key=lambda x: x.get(sort_key, ""), reverse=reverse)

    return render_template(
        "dashboard.html",
        players=players,
        team_names=sorted(set(teams)),
        selected_team=selected_team,
        search=search,
        sort_dir=sort_dir
    )

if __name__ == "__main__":
    app.run(debug=True)
