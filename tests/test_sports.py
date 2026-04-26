"""Tests for src/sports.py — mocks httpx.Client."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import httpx
import pytest

from src import sports
from src.sports import (
    GameResult,
    TeamSnapshot,
    fetch_mlb_team,
    fetch_nba_playoffs,
    fetch_nba_team,
    fetch_nfl_playoffs,
    fetch_nfl_team,
    fetch_sports_section,
    render_markdown,
)

YESTERDAY = date(2026, 4, 24)


def _resp(status: int = 200, json_data: dict | None = None) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.json.return_value = json_data or {}
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"{status}", request=MagicMock(), response=r,
        )
    else:
        r.raise_for_status.return_value = None
    return r


def _client(*responses: MagicMock) -> MagicMock:
    """httpx.Client whose .get() returns the given responses in order."""
    c = MagicMock(spec=httpx.Client)
    if len(responses) == 1:
        c.get.return_value = responses[0]
    else:
        c.get.side_effect = list(responses)
    return c


# ---------- MLB ----------

MLB_BREWERS_GAME = {
    "gameType": "R",
    "seriesDescription": None,
    "status": {"abstractGameState": "Final"},
    "teams": {
        "home": {"team": {"name": "Milwaukee Brewers", "id": 158}, "score": 5},
        "away": {"team": {"name": "Chicago Cubs", "id": 112}, "score": 3},
    },
    "decisions": {
        "winner": {"fullName": "Freddy Peralta"},
        "loser": {"fullName": "Justin Steele"},
        "save": {"fullName": "Devin Williams"},
    },
}


def test_mlb_yesterday_with_decisions():
    payload = {"dates": [{"date": "2026-04-24", "games": [MLB_BREWERS_GAME]}]}
    snap = fetch_mlb_team("Milwaukee Brewers", 158, YESTERDAY,
                          client=_client(_resp(200, payload)))
    assert len(snap.games_yesterday) == 1
    g = snap.games_yesterday[0]
    assert g.status == "final"
    assert g.home_score == 5 and g.away_score == 3
    assert "Winning pitcher: Freddy Peralta" in g.notes
    assert "Save: Devin Williams" in g.notes
    assert g.is_postseason is False


def test_mlb_finds_next_game_when_no_yesterday():
    payload = {"dates": [
        {"date": "2026-04-26", "games": [{
            "gameType": "R", "status": {"abstractGameState": "Preview"},
            "teams": {
                "home": {"team": {"name": "Milwaukee Brewers"}, "score": 0},
                "away": {"team": {"name": "Pittsburgh Pirates"}, "score": 0},
            },
        }]},
    ]}
    snap = fetch_mlb_team("Milwaukee Brewers", 158, YESTERDAY,
                          client=_client(_resp(200, payload)))
    assert snap.games_yesterday == []
    assert snap.next_game_date == date(2026, 4, 26)
    assert snap.next_opponent == "Pittsburgh Pirates"
    assert snap.in_season is True


def test_mlb_off_season_returns_empty_snapshot():
    snap = fetch_mlb_team("Milwaukee Brewers", 158, YESTERDAY,
                          client=_client(_resp(200, {"dates": []})))
    assert snap.games_yesterday == []
    assert snap.next_game_date is None
    assert snap.in_season is False


def test_mlb_postseason_marked():
    g = dict(MLB_BREWERS_GAME, gameType="L", seriesDescription="NLCS")
    payload = {"dates": [{"date": "2026-04-24", "games": [g]}]}
    snap = fetch_mlb_team("Milwaukee Brewers", 158, YESTERDAY,
                          client=_client(_resp(200, payload)))
    assert snap.games_yesterday[0].is_postseason
    assert snap.games_yesterday[0].series_label == "NLCS"


def test_mlb_http_failure_returns_empty(monkeypatch):
    """Tenacity retries 3x then raises; orchestrator catches and returns empty."""
    err_client = MagicMock(spec=httpx.Client)
    err_client.get.side_effect = httpx.ConnectError("network down")
    snap = fetch_mlb_team("Milwaukee Brewers", 158, YESTERDAY, client=err_client)
    assert snap.games_yesterday == []
    assert snap.in_season is False


# ---------- balldontlie (NBA) ----------

NBA_BUCKS_GAME = {
    "date": "2026-04-24T00:00:00.000Z",
    "status": "Final",
    "home_team": {"full_name": "Milwaukee Bucks"},
    "visitor_team": {"full_name": "Boston Celtics"},
    "home_team_score": 108,
    "visitor_team_score": 112,
    "postseason": True,
}


def test_nba_team_requires_api_key(monkeypatch):
    monkeypatch.delenv("BALLDONTLIE_API_KEY", raising=False)
    snap = fetch_nba_team("Milwaukee Bucks", 17, YESTERDAY,
                          client=MagicMock(spec=httpx.Client))
    assert snap.games_yesterday == []
    assert snap.in_season is False


def test_nba_team_with_key_parses_game(monkeypatch):
    monkeypatch.setenv("BALLDONTLIE_API_KEY", "test-key")
    payload = {"data": [NBA_BUCKS_GAME]}
    client = _client(_resp(200, payload))
    snap = fetch_nba_team("Milwaukee Bucks", 17, YESTERDAY, client=client)

    assert len(snap.games_yesterday) == 1
    g = snap.games_yesterday[0]
    assert g.status == "final"
    assert g.home_team == "Milwaukee Bucks"
    assert g.away_team == "Boston Celtics"
    assert g.home_score == 108 and g.away_score == 112
    assert g.is_postseason is True

    # Verify Authorization header was set.
    headers = client.get.call_args.kwargs["headers"]
    assert headers == {"Authorization": "test-key"}


def test_nba_playoffs_filter(monkeypatch):
    monkeypatch.setenv("BALLDONTLIE_API_KEY", "k")
    payload = {"data": [NBA_BUCKS_GAME]}
    games = fetch_nba_playoffs(YESTERDAY, client=_client(_resp(200, payload)))
    assert len(games) == 1
    assert games[0].is_postseason is True


def test_nba_playoffs_no_key(monkeypatch):
    monkeypatch.delenv("BALLDONTLIE_API_KEY", raising=False)
    assert fetch_nba_playoffs(YESTERDAY,
                              client=MagicMock(spec=httpx.Client)) == []


# ---------- TheSportsDB (NFL) ----------

NFL_PACKERS_LAST = {
    "dateEvent": "2026-04-24",
    "strHomeTeam": "Green Bay Packers",
    "strAwayTeam": "Chicago Bears",
    "intHomeScore": "27",
    "intAwayScore": "20",
    "intRound": "10",  # regular season
    "strStatus": "FT",
    "strLeague": "NFL",
}

NFL_PACKERS_NEXT = {
    "dateEvent": "2026-04-28",
    "strHomeTeam": "Detroit Lions",
    "strAwayTeam": "Green Bay Packers",
    "intRound": "11",
    "strStatus": "NS",
    "strLeague": "NFL",
}


def test_nfl_team_yesterday_and_next():
    last_resp = _resp(200, {"results": [NFL_PACKERS_LAST]})
    next_resp = _resp(200, {"events": [NFL_PACKERS_NEXT]})
    snap = fetch_nfl_team("Green Bay Packers", 134940, YESTERDAY,
                          client=_client(last_resp, next_resp))
    assert len(snap.games_yesterday) == 1
    g = snap.games_yesterday[0]
    assert g.status == "final"
    assert g.home_score == 27 and g.away_score == 20
    assert g.is_postseason is False
    assert snap.next_game_date == date(2026, 4, 28)
    assert snap.next_opponent == "Detroit Lions"


def test_nfl_team_off_season_when_no_results():
    last_resp = _resp(200, {"results": None})
    next_resp = _resp(200, {"events": None})
    snap = fetch_nfl_team("Green Bay Packers", 134940, YESTERDAY,
                          client=_client(last_resp, next_resp))
    assert not snap.in_season


def test_nfl_team_filters_cross_sport_collisions():
    """TheSportsDB sometimes returns events from other leagues for an NFL id."""
    last_resp = _resp(200, {"results": [
        {"dateEvent": "2026-04-24", "strHomeTeam": "Bolton Wanderers",
         "strAwayTeam": "Luton Town", "intHomeScore": "1", "intAwayScore": "0",
         "intRound": "33", "strStatus": "FT", "strLeague": "English League 1"},
    ]})
    next_resp = _resp(200, {"events": [
        {"dateEvent": "2026-05-02", "strHomeTeam": "Bolton Wanderers",
         "strAwayTeam": "Luton Town", "intRound": "34", "strStatus": "NS",
         "strLeague": "English League 1"},
        {"dateEvent": "2026-09-08", "strHomeTeam": "Green Bay Packers",
         "strAwayTeam": "Chicago Bears", "intRound": "1", "strStatus": "NS",
         "strLeague": "NFL"},
    ]})
    snap = fetch_nfl_team("Green Bay Packers", 134940, YESTERDAY,
                          client=_client(last_resp, next_resp))
    assert snap.games_yesterday == []
    assert snap.next_game_date == date(2026, 9, 8)
    assert snap.next_opponent == "Chicago Bears"


def test_nfl_team_eventslast_failure_doesnt_break_next():
    """If eventslast fails, eventsnext should still populate next_game."""
    err_client = MagicMock(spec=httpx.Client)
    err_client.get.side_effect = [
        httpx.ConnectError("flaky"),
        httpx.ConnectError("flaky"),
        httpx.ConnectError("flaky"),  # 3 retries on first call
        _resp(200, {"events": [NFL_PACKERS_NEXT]}),
    ]
    snap = fetch_nfl_team("Green Bay Packers", 134940, YESTERDAY, client=err_client)
    assert snap.games_yesterday == []
    assert snap.next_game_date == date(2026, 4, 28)


def test_nfl_playoffs_filters_by_round():
    payload = {"events": [
        {"dateEvent": "2026-04-24", "strHomeTeam": "Chiefs", "strAwayTeam": "Bills",
         "intHomeScore": "28", "intAwayScore": "21", "intRound": "150",
         "strStatus": "FT", "strLeague": "NFL"},
        {"dateEvent": "2026-04-24", "strHomeTeam": "Jets", "strAwayTeam": "Pats",
         "intHomeScore": "10", "intAwayScore": "13", "intRound": "5",
         "strStatus": "FT", "strLeague": "NFL"},  # regular season — filtered out
    ]}
    games = fetch_nfl_playoffs(YESTERDAY, client=_client(_resp(200, payload)))
    assert len(games) == 1
    g = games[0]
    assert g.home_team == "Chiefs"
    assert g.is_postseason is True
    assert g.series_label == "Divisional"


def test_nfl_playoffs_empty_when_no_events():
    games = fetch_nfl_playoffs(YESTERDAY,
                               client=_client(_resp(200, {"events": None})))
    assert games == []


# ---------- Orchestrator ----------

def test_orchestrator_assembles_section(monkeypatch):
    """Wire up MLB-only flow with a single team and no playoffs."""
    monkeypatch.delenv("BALLDONTLIE_API_KEY", raising=False)

    config = {
        "teams": [{"name": "Milwaukee Brewers", "league": "MLB",
                   "api": "mlb_stats", "team_id": 158}],
        "playoffs": [],
    }
    payload = {"dates": [{"date": "2026-04-24", "games": [MLB_BREWERS_GAME]}]}
    client = _client(_resp(200, payload))

    section = fetch_sports_section(YESTERDAY, client=client, config=config)
    assert len(section["teams"]) == 1
    assert section["teams"][0].team_name == "Milwaukee Brewers"
    assert section["playoffs"] == {}


def test_orchestrator_drops_off_season_teams(monkeypatch):
    monkeypatch.delenv("BALLDONTLIE_API_KEY", raising=False)
    config = {
        "teams": [{"name": "Milwaukee Brewers", "league": "MLB",
                   "api": "mlb_stats", "team_id": 158}],
        "playoffs": [],
    }
    client = _client(_resp(200, {"dates": []}))
    section = fetch_sports_section(YESTERDAY, client=client, config=config)
    assert section["teams"] == []


# ---------- Rendering ----------

def test_render_team_game():
    snap = TeamSnapshot(
        team_name="Milwaukee Brewers", league="MLB",
        games_yesterday=[GameResult(
            league="MLB", home_team="Milwaukee Brewers", away_team="Chicago Cubs",
            home_score=5, away_score=3, status="final",
            notes="Winning pitcher: Freddy Peralta.",
        )],
    )
    md = render_markdown({"teams": [snap], "playoffs": {}}, YESTERDAY)
    assert "Milwaukee Brewers 5, Chicago Cubs 3 (final)" in md
    assert "Winning pitcher: Freddy Peralta." in md


def test_render_no_game_yesterday_shows_next():
    snap = TeamSnapshot(
        team_name="Milwaukee Bucks", league="NBA", games_yesterday=[],
        next_game_date=date(2026, 4, 27), next_opponent="Boston Celtics",
    )
    md = render_markdown({"teams": [snap], "playoffs": {}}, YESTERDAY)
    assert "no game yesterday" in md
    assert "next: 2026-04-27 vs Boston Celtics" in md


def test_render_playoffs_subsection():
    games = [GameResult(
        league="NBA", home_team="Boston Celtics", away_team="Miami Heat",
        home_score=112, away_score=108, status="final",
        is_postseason=True, series_label="Eastern Conference First Round",
    )]
    md = render_markdown({"teams": [], "playoffs": {"NBA": games}}, YESTERDAY)
    assert "### NBA Playoffs" in md
    assert "Boston Celtics 112, Miami Heat 108 (final)" in md
    assert "Eastern Conference First Round" in md


def test_render_empty_section():
    md = render_markdown({"teams": [], "playoffs": {}}, YESTERDAY)
    assert "No tracked teams in season" in md


def test_render_orders_playoffs_nba_then_nfl():
    nba = [GameResult(league="NBA", home_team="A", away_team="B",
                       home_score=1, away_score=0, status="final",
                       is_postseason=True)]
    nfl = [GameResult(league="NFL", home_team="C", away_team="D",
                       home_score=1, away_score=0, status="final",
                       is_postseason=True, series_label="Wild Card")]
    md = render_markdown({"teams": [], "playoffs": {"NFL": nfl, "NBA": nba}}, YESTERDAY)
    assert md.index("NBA Playoffs") < md.index("NFL Playoffs")


# ---------- Pure helpers ----------

def test_nfl_playoff_round_detection():
    from src.sports import _is_nfl_playoff_round, _nfl_round_label
    assert not _is_nfl_playoff_round(None)
    assert not _is_nfl_playoff_round("0")
    assert not _is_nfl_playoff_round("17")
    assert _is_nfl_playoff_round("125")
    assert _is_nfl_playoff_round(200)
    assert _nfl_round_label(125) == "Wild Card"
    assert _nfl_round_label(200) == "Super Bowl"
    assert _nfl_round_label("garbage") is None


def test_load_teams_config_round_trip():
    cfg = sports.load_teams_config()
    names = [t["name"] for t in cfg["teams"]]
    assert "Milwaukee Brewers" in names
    assert "Green Bay Packers" in names
    assert any(p["league"] == "NBA" for p in cfg["playoffs"])
