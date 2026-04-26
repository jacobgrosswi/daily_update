"""Sports section: per-team yesterday's results + league playoffs.

Three free providers:
  - MLB Stats API (statsapi.mlb.com)        — Brewers
  - balldontlie.io (BALLDONTLIE_API_KEY)    — Bucks + NBA playoffs
  - TheSportsDB (key '3')                    — Packers + NFL playoffs

Off-season behavior per scoping doc 4.2: if a tracked team has no game
yesterday AND no game in the next 7 days, omit it entirely. Playoff
sub-sections are omitted when the league has no postseason games.

balldontlie note: their public API now requires a free API key (this
changed after the scoping doc was written). If BALLDONTLIE_API_KEY is
unset, NBA fetches log a warning and degrade to "section unavailable".
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import httpx
import yaml
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .utils import REPO_ROOT, get_logger

log = get_logger(__name__)

TEAMS_PATH = REPO_ROOT / "config" / "teams.yml"

MLB_BASE = "https://statsapi.mlb.com/api/v1"
BALLDONTLIE_BASE = "https://api.balldontlie.io/v1"
SPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/3"

# NFL league ID in TheSportsDB.
SPORTSDB_NFL_LEAGUE_ID = "4391"

# MLB game types treated as postseason.
MLB_POSTSEASON_TYPES = {"F", "D", "L", "W"}  # WildCard, Division, League Champ, World Series

LOOKAHEAD_DAYS = 7


# ---------- Data models ----------

@dataclass
class GameResult:
    """One played game (final or in-progress)."""
    league: str          # 'MLB' | 'NBA' | 'NFL'
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    status: str          # 'final' | 'in_progress' | 'scheduled'
    is_postseason: bool = False
    series_label: Optional[str] = None   # e.g. "Eastern Conference First Round"
    notes: Optional[str] = None          # e.g. "Winning pitcher: J. Doe"


@dataclass
class TeamSnapshot:
    """What we'll show for one tracked team in the briefing."""
    team_name: str
    league: str
    games_yesterday: list[GameResult]    # usually 0 or 1
    next_game_date: Optional[date] = None
    next_opponent: Optional[str] = None

    @property
    def in_season(self) -> bool:
        return bool(self.games_yesterday) or self.next_game_date is not None


# ---------- Config ----------

def load_teams_config(path: Path = TEAMS_PATH) -> dict:
    """Returns {'teams': [...], 'playoffs': [...]}."""
    return yaml.safe_load(path.read_text())


# ---------- HTTP helpers ----------

def _retry_http():
    return retry(
        retry=retry_if_exception_type((httpx.HTTPError,)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )


@_retry_http()
def _get_json(client: httpx.Client, url: str, *, params: Optional[dict] = None,
              headers: Optional[dict] = None) -> dict:
    r = client.get(url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


# ---------- MLB ----------

def fetch_mlb_team(team_name: str, team_id: int, target_date: date,
                   *, client: httpx.Client) -> TeamSnapshot:
    """Fetch yesterday + next 7 days for an MLB team."""
    end = target_date + timedelta(days=LOOKAHEAD_DAYS)
    params = {
        "sportId": "1",
        "teamId": str(team_id),
        "startDate": target_date.isoformat(),
        "endDate": end.isoformat(),
        "hydrate": "decisions",
    }
    try:
        data = _get_json(client, f"{MLB_BASE}/schedule", params=params)
    except httpx.HTTPError as e:
        log.warning("MLB fetch failed for %s: %s", team_name, e)
        return TeamSnapshot(team_name=team_name, league="MLB", games_yesterday=[])

    yesterday: list[GameResult] = []
    next_date: Optional[date] = None
    next_opp: Optional[str] = None

    for d in data.get("dates", []):
        d_date = date.fromisoformat(d["date"])
        for g in d.get("games", []):
            if d_date == target_date:
                yesterday.append(_parse_mlb_game(g))
            elif d_date > target_date and next_date is None:
                next_date = d_date
                home = g["teams"]["home"]["team"]["name"]
                away = g["teams"]["away"]["team"]["name"]
                next_opp = away if home == team_name else home

    return TeamSnapshot(team_name=team_name, league="MLB",
                        games_yesterday=yesterday,
                        next_game_date=next_date, next_opponent=next_opp)


def _parse_mlb_game(g: dict) -> GameResult:
    home = g["teams"]["home"]
    away = g["teams"]["away"]
    state = (g["status"]["abstractGameState"] or "").lower()
    status = {"final": "final", "live": "in_progress"}.get(state, "scheduled")
    is_post = g.get("gameType") in MLB_POSTSEASON_TYPES

    notes = None
    decisions = g.get("decisions") or {}
    winner = (decisions.get("winner") or {}).get("fullName")
    save = (decisions.get("save") or {}).get("fullName")
    if winner:
        notes = f"Winning pitcher: {winner}."
        if save:
            notes += f" Save: {save}."

    return GameResult(
        league="MLB",
        home_team=home["team"]["name"],
        away_team=away["team"]["name"],
        home_score=int(home.get("score") or 0),
        away_score=int(away.get("score") or 0),
        status=status,
        is_postseason=is_post,
        series_label=g.get("seriesDescription") if is_post else None,
        notes=notes,
    )


# ---------- balldontlie (NBA) ----------

def _balldontlie_headers() -> Optional[dict]:
    key = os.environ.get("BALLDONTLIE_API_KEY")
    return {"Authorization": key} if key else None


def fetch_nba_team(team_name: str, team_id: int, target_date: date,
                   *, client: httpx.Client) -> TeamSnapshot:
    headers = _balldontlie_headers()
    if headers is None:
        log.warning("BALLDONTLIE_API_KEY not set; skipping NBA team fetch.")
        return TeamSnapshot(team_name=team_name, league="NBA", games_yesterday=[])

    end = target_date + timedelta(days=LOOKAHEAD_DAYS)
    params = {
        "team_ids[]": str(team_id),
        "start_date": target_date.isoformat(),
        "end_date": end.isoformat(),
        "per_page": "100",
    }
    try:
        data = _get_json(client, f"{BALLDONTLIE_BASE}/games",
                         params=params, headers=headers)
    except httpx.HTTPError as e:
        log.warning("balldontlie team fetch failed for %s: %s", team_name, e)
        return TeamSnapshot(team_name=team_name, league="NBA", games_yesterday=[])

    yesterday: list[GameResult] = []
    next_date: Optional[date] = None
    next_opp: Optional[str] = None

    for g in data.get("data", []):
        g_date = date.fromisoformat(g["date"][:10])
        if g_date == target_date:
            yesterday.append(_parse_balldontlie_game(g))
        elif g_date > target_date and next_date is None:
            next_date = g_date
            home = g["home_team"]["full_name"]
            away = g["visitor_team"]["full_name"]
            next_opp = away if home == team_name else home

    return TeamSnapshot(team_name=team_name, league="NBA",
                        games_yesterday=yesterday,
                        next_game_date=next_date, next_opponent=next_opp)


def fetch_nba_playoffs(target_date: date, *, client: httpx.Client) -> list[GameResult]:
    headers = _balldontlie_headers()
    if headers is None:
        log.warning("BALLDONTLIE_API_KEY not set; skipping NBA playoffs.")
        return []

    params = {
        "dates[]": target_date.isoformat(),
        "postseason": "true",
        "per_page": "100",
    }
    try:
        data = _get_json(client, f"{BALLDONTLIE_BASE}/games",
                         params=params, headers=headers)
    except httpx.HTTPError as e:
        log.warning("balldontlie playoffs fetch failed: %s", e)
        return []
    return [_parse_balldontlie_game(g) for g in data.get("data", [])]


def _parse_balldontlie_game(g: dict) -> GameResult:
    raw_status = (g.get("status") or "").lower()
    if raw_status == "final":
        status = "final"
    elif raw_status in ("scheduled", ""):
        status = "scheduled"
    else:
        status = "in_progress"

    return GameResult(
        league="NBA",
        home_team=g["home_team"]["full_name"],
        away_team=g["visitor_team"]["full_name"],
        home_score=int(g.get("home_team_score") or 0),
        away_score=int(g.get("visitor_team_score") or 0),
        status=status,
        is_postseason=bool(g.get("postseason", False)),
    )


# ---------- TheSportsDB (NFL) ----------

def fetch_nfl_team(team_name: str, team_id: int, target_date: date,
                   *, client: httpx.Client) -> TeamSnapshot:
    yesterday: list[GameResult] = []
    next_date: Optional[date] = None
    next_opp: Optional[str] = None

    # Last games (covers yesterday). TheSportsDB sometimes returns events
    # from other sports against the same id — filter to NFL events that
    # actually involve this team.
    try:
        last = _get_json(client, f"{SPORTSDB_BASE}/eventslast.php",
                         params={"id": str(team_id)})
        for ev in last.get("results") or []:
            if not _is_nfl_event_for(ev, team_name):
                continue
            if ev.get("dateEvent") == target_date.isoformat():
                yesterday.append(_parse_sportsdb_event(ev, league="NFL"))
    except httpx.HTTPError as e:
        log.warning("TheSportsDB eventslast failed for %s: %s", team_name, e)

    # Next games (off-season check + next opponent).
    try:
        nxt = _get_json(client, f"{SPORTSDB_BASE}/eventsnext.php",
                        params={"id": str(team_id)})
        for ev in nxt.get("events") or []:
            if not _is_nfl_event_for(ev, team_name):
                continue
            d = date.fromisoformat(ev["dateEvent"])
            if d > target_date:
                next_date = d
                home, away = ev["strHomeTeam"], ev["strAwayTeam"]
                next_opp = away if home == team_name else home
                break
    except httpx.HTTPError as e:
        log.warning("TheSportsDB eventsnext failed for %s: %s", team_name, e)

    return TeamSnapshot(team_name=team_name, league="NFL",
                        games_yesterday=yesterday,
                        next_game_date=next_date, next_opponent=next_opp)


def fetch_nfl_playoffs(target_date: date, *, client: httpx.Client) -> list[GameResult]:
    """NFL playoff games for a given date.

    TheSportsDB eventsday returns games for the league on a date; we filter
    by `intRound` to keep only postseason rounds (NFL playoff rounds are
    exposed as 125/150/160/200 — anything outside the regular weekly range).
    """
    try:
        data = _get_json(client, f"{SPORTSDB_BASE}/eventsday.php",
                         params={"d": target_date.isoformat(), "l": "NFL"})
    except httpx.HTTPError as e:
        log.warning("TheSportsDB eventsday failed: %s", e)
        return []

    out: list[GameResult] = []
    for ev in data.get("events") or []:
        if not _is_nfl_playoff_round(ev.get("intRound")):
            continue
        out.append(_parse_sportsdb_event(ev, league="NFL", is_postseason=True))
    return out


def _is_nfl_event_for(ev: dict, team_name: str) -> bool:
    """Defend against TheSportsDB cross-sport id collisions."""
    if (ev.get("strLeague") or "").upper() != "NFL":
        return False
    return team_name in (ev.get("strHomeTeam"), ev.get("strAwayTeam"))


def _is_nfl_playoff_round(intround: Optional[str | int]) -> bool:
    """NFL regular season rounds are 1-18. Playoff rounds use codes ≥ 125."""
    if intround is None:
        return False
    try:
        return int(intround) >= 125
    except (ValueError, TypeError):
        return False


def _parse_sportsdb_event(ev: dict, *, league: str,
                          is_postseason: bool = False) -> GameResult:
    raw_status = (ev.get("strStatus") or "").upper()
    if raw_status in ("FT", "AOT", "AET"):  # full time / after overtime
        status = "final"
    elif raw_status in ("NS", "POSTPONED", "", "TBD"):
        status = "scheduled"
    else:
        status = "in_progress"

    series = None
    if is_postseason:
        series = _nfl_round_label(ev.get("intRound"))

    return GameResult(
        league=league,
        home_team=ev["strHomeTeam"],
        away_team=ev["strAwayTeam"],
        home_score=int(ev.get("intHomeScore") or 0),
        away_score=int(ev.get("intAwayScore") or 0),
        status=status,
        is_postseason=is_postseason,
        series_label=series,
    )


def _nfl_round_label(intround: Optional[str | int]) -> Optional[str]:
    try:
        n = int(intround) if intround is not None else None
    except (ValueError, TypeError):
        return None
    return {
        125: "Wild Card",
        150: "Divisional",
        160: "Conference Championship",
        200: "Super Bowl",
    }.get(n)


# ---------- Orchestrator ----------

def fetch_sports_section(target_date: date,
                         *, client: Optional[httpx.Client] = None,
                         config: Optional[dict] = None) -> dict:
    """Fetch all per-team snapshots and playoff results for one date.

    Returns:
        {
            "teams": [TeamSnapshot, ...],   # in-season teams only
            "playoffs": {"NBA": [...], "NFL": [...]},  # only non-empty leagues
        }
    """
    cfg = config if config is not None else load_teams_config()
    own_client = False
    if client is None:
        client = httpx.Client()
        own_client = True

    try:
        teams: list[TeamSnapshot] = []
        for t in cfg.get("teams", []):
            api = t["api"]
            if api == "mlb_stats":
                snap = fetch_mlb_team(t["name"], int(t["team_id"]), target_date, client=client)
            elif api == "balldontlie":
                snap = fetch_nba_team(t["name"], int(t["team_id"]), target_date, client=client)
            elif api == "thesportsdb":
                snap = fetch_nfl_team(t["name"], int(t["team_id"]), target_date, client=client)
            else:
                log.warning("Unknown team api: %s", api)
                continue
            if snap.in_season:
                teams.append(snap)

        playoffs: dict[str, list[GameResult]] = {}
        for p in cfg.get("playoffs", []):
            league, api = p["league"], p["api"]
            if api == "balldontlie":
                games = fetch_nba_playoffs(target_date, client=client)
            elif api == "thesportsdb":
                games = fetch_nfl_playoffs(target_date, client=client)
            else:
                log.warning("Unknown playoff api: %s", api)
                continue
            if games:
                playoffs[league] = games

        return {"teams": teams, "playoffs": playoffs}
    finally:
        if own_client:
            client.close()


# ---------- Rendering ----------

def render_markdown(section: dict, target_date: date) -> str:
    teams: list[TeamSnapshot] = section.get("teams") or []
    playoffs: dict[str, list[GameResult]] = section.get("playoffs") or {}

    if not teams and not playoffs:
        return "## Sports\n\nNo tracked teams in season; no playoff games yesterday.\n"

    lines = ["## Sports", ""]

    for snap in teams:
        if snap.games_yesterday:
            for g in snap.games_yesterday:
                lines.append(_format_team_game(snap.team_name, g))
                if g.notes:
                    lines.append(f"  {g.notes}")
            lines.append("")
        else:
            nxt = snap.next_game_date.isoformat() if snap.next_game_date else "TBD"
            opp = snap.next_opponent or "TBD"
            lines.append(f"{snap.team_name}: no game yesterday "
                         f"(next: {nxt} vs {opp})")
            lines.append("")

    for league in ("NBA", "NFL"):
        games = playoffs.get(league)
        if not games:
            continue
        lines.append(f"### {league} Playoffs")
        for g in games:
            lines.append(f"- {_format_playoff_game(g)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _format_team_game(team_name: str, g: GameResult) -> str:
    """'Brewers 5, Cubs 3 (final)' style — tracked team listed first."""
    if team_name in (g.home_team, g.away_team):
        if team_name == g.home_team:
            us, them, us_score, them_score = g.home_team, g.away_team, g.home_score, g.away_score
        else:
            us, them, us_score, them_score = g.away_team, g.home_team, g.away_score, g.home_score
    else:
        us, them, us_score, them_score = g.home_team, g.away_team, g.home_score, g.away_score
    return f"{us} {us_score}, {them} {them_score} ({g.status})"


def _format_playoff_game(g: GameResult) -> str:
    base = f"{g.home_team} {g.home_score}, {g.away_team} {g.away_score} ({g.status})"
    if g.series_label:
        return f"{base} — {g.series_label}"
    return base
