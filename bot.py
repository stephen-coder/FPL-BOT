import os
import sqlite3
import asyncio
import threading
import time
import logging
from typing import Optional

import requests
import pulp

from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

WEBHOOK_URL = os.getenv(
    "WEBHOOK_URL",
    "https://your-render-app.onrender.com/webhook"
)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

PORT = int(os.getenv("PORT", "5000"))

# Live polling interval.
LIVE_POLL_SECONDS = int(
    os.getenv("LIVE_POLL_SECONDS", "60")
)

# Normal FPL data cache.
FPL_CACHE_SECONDS = 300

DATABASE = "fpl_bot.db"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# TELEGRAM
# ============================================================

if not TOKEN:
    logger.warning(
        "TELEGRAM_BOT_TOKEN is not configured."
    )

application = (
    Application.builder()
    .token(TOKEN if TOKEN else "000000:INVALID")
    .updater(None)
    .build()
)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    return sqlite3.connect(
        DATABASE,
        timeout=30,
        check_same_thread=False
    )


def init_db():

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_teams (
            chat_id INTEGER PRIMARY KEY,
            team_id INTEGER NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def save_team_id(chat_id, team_id):

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO user_teams(chat_id, team_id)
        VALUES (?, ?)
        ON CONFLICT(chat_id)
        DO UPDATE SET team_id = excluded.team_id
    """, (chat_id, team_id))

    conn.commit()
    conn.close()


def get_team_id(chat_id):

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT team_id
        FROM user_teams
        WHERE chat_id = ?
    """, (chat_id,))

    row = cursor.fetchone()

    conn.close()

    return row[0] if row else None


def get_all_user_teams():

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT chat_id, team_id
        FROM user_teams
    """)

    rows = cursor.fetchall()

    conn.close()

    return rows


init_db()


# ============================================================
# FPL API
# ============================================================

FPL_BASE = "https://fantasy.premierleague.com/api"

_cache = {
    "bootstrap": None,
    "fixtures": None,
    "timestamp": 0
}

_cache_lock = threading.Lock()


def fpl_get(endpoint):

    url = f"{FPL_BASE}/{endpoint.lstrip('/')}"

    try:

        response = requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent": "FPL-Tactical-Assistant/2.0"
            }
        )

        response.raise_for_status()

        return response.json()

    except requests.RequestException as exc:

        logger.error(
            "FPL request failed: %s",
            exc
        )

        return None

    except ValueError as exc:

        logger.error(
            "Invalid FPL JSON: %s",
            exc
        )

        return None


def get_fpl_data(force_refresh=False):

    now = time.time()

    with _cache_lock:

        if (
            not force_refresh
            and _cache["bootstrap"] is not None
            and _cache["fixtures"] is not None
            and now - _cache["timestamp"]
            < FPL_CACHE_SECONDS
        ):
            return (
                _cache["bootstrap"],
                _cache["fixtures"]
            )

    bootstrap = fpl_get(
        "/bootstrap-static/"
    )

    fixtures = fpl_get(
        "/fixtures/"
    )

    if bootstrap is None or fixtures is None:
        return None, None

    with _cache_lock:

        _cache["bootstrap"] = bootstrap
        _cache["fixtures"] = fixtures
        _cache["timestamp"] = now

    return bootstrap, fixtures


def get_current_gameweek(bootstrap):

    events = bootstrap.get(
        "events",
        []
    )

    current = next(
        (
            gw["id"]
            for gw in events
            if gw.get("is_current")
        ),
        None
    )

    if current:
        return current

    upcoming = next(
        (
            gw["id"]
            for gw in events
            if gw.get("is_next")
        ),
        None
    )

    if upcoming:
        return upcoming

    return 1


def fetch_user_entry(team_id):

    return fpl_get(
        f"/entry/{team_id}/"
    )


def fetch_user_picks(team_id):

    bootstrap, _ = get_fpl_data()

    if not bootstrap:
        return None, None

    gameweek = get_current_gameweek(
        bootstrap
    )

    picks = fpl_get(
        f"/entry/{team_id}/event/{gameweek}/picks/"
    )

    if not picks:
        return None, gameweek

    return picks, gameweek


def fetch_live_gameweek(gameweek):

    return fpl_get(
        f"/event/{gameweek}/live/"
    )


# ============================================================
# HELPERS
# ============================================================

POSITION_NAMES = {
    1: "GKP",
    2: "DEF",
    3: "MID",
    4: "FWD"
}


def player_name(player):
    return player.get(
        "web_name",
        "Unknown"
    )


def player_price(player):
    return player.get(
        "now_cost",
        0
    ) / 10


def safe_float(value, default=0):

    try:
        return float(value)
    except (
        TypeError,
        ValueError
    ):
        return default


def player_expected_points(player):

    return safe_float(
        player.get("ep_next"),
        0
    )


def player_fit(player):

    chance = player.get(
        "chance_of_playing_next_round"
    )

    if chance is None:
        return True

    return safe_float(chance) > 0


def minutes_probability(player):

    chance = player.get(
        "chance_of_playing_next_round"
    )

    if chance is not None:
        return max(
            0,
            min(
                safe_float(chance, 100),
                100
            )
        ) / 100

    starts = safe_float(
        player.get("starts"),
        0
    )

    minutes = safe_float(
        player.get("minutes"),
        0
    )

    if minutes <= 0:
        return 0.75

    # Rough historical start/minutes indicator.
    estimated = min(
        1.0,
        max(
            0.55,
            starts / max(minutes / 90, 1)
        )
    )

    return estimated


def fixture_difficulty(
    fixture,
    team_id
):

    if fixture.get("team_h") == team_id:

        return safe_float(
            fixture.get(
                "team_h_difficulty",
                3
            ),
            3
        )

    return safe_float(
        fixture.get(
            "team_a_difficulty",
            3
        ),
        3
    )


def get_team_name(
    bootstrap,
    team_id
):

    team = next(
        (
            t for t in bootstrap["teams"]
            if t["id"] == team_id
        ),
        None
    )

    return (
        team["name"]
        if team
        else "Unknown"
    )


def opponent_name(
    bootstrap,
    fixture,
    team_id
):

    if fixture.get("team_h") == team_id:
        opponent_id = fixture.get("team_a")
    else:
        opponent_id = fixture.get("team_h")

    return get_team_name(
        bootstrap,
        opponent_id
    )


def fixture_label(
    bootstrap,
    fixture,
    team_id
):

    home = (
        fixture.get("team_h")
        == team_id
    )

    opponent = opponent_name(
        bootstrap,
        fixture,
        team_id
    )

    return (
        f"{opponent} "
        f"({'H' if home else 'A'})"
    )


def get_upcoming_fixtures(
    bootstrap,
    fixtures,
    team_id,
    limit=3
):

    upcoming = []

    for fixture in fixtures:

        if fixture.get("finished"):
            continue

        if (
            fixture.get("team_h")
            != team_id
            and
            fixture.get("team_a")
            != team_id
        ):
            continue

        upcoming.append(
            fixture
        )

    upcoming.sort(
        key=lambda f: (
            f.get("event") is None,
            f.get("event") or 999
        )
    )

    return upcoming[:limit]


# ============================================================
# NEW: PLAYER PREDICTION ENGINE
# ============================================================

def calculate_fixture_score(
    bootstrap,
    fixtures,
    player,
    gameweeks=1
):

    upcoming = get_upcoming_fixtures(
        bootstrap,
        fixtures,
        player["team"],
        limit=gameweeks
    )

    if not upcoming:
        return 1.0

    score = 0

    for fixture in upcoming:

        fdr = fixture_difficulty(
            fixture,
            player["team"]
        )

        # FDR 1 = excellent
        # FDR 5 = difficult.
        difficulty_factor = (
            1.25 - ((fdr - 1) * 0.12)
        )

        difficulty_factor = max(
            0.65,
            min(
                difficulty_factor,
                1.25
            )
        )

        score += difficulty_factor

    return score / len(upcoming)


def player_prediction_score(
    bootstrap,
    fixtures,
    player,
    gameweeks=1
):

    """
    Composite prediction model.

    It intentionally combines several FPL-provided metrics
    rather than blindly trusting ep_next.
    """

    ep = player_expected_points(
        player
    )

    form = safe_float(
        player.get("form"),
        0
    )

    ppg = safe_float(
        player.get("points_per_game"),
        0
    )

    xgi = safe_float(
        player.get(
            "expected_goal_involvements"
        ),
        0
    )

    threat = safe_float(
        player.get("threat"),
        0
    )

    creativity = safe_float(
        player.get("creativity"),
        0
    )

    influence = safe_float(
        player.get("influence"),
        0
    )

    minutes = minutes_probability(
        player
    )

    fixture_score = calculate_fixture_score(
        bootstrap,
        fixtures,
        player,
        gameweeks
    )

    # Normalize some metrics.
    xgi_component = min(
        xgi * 0.45,
        2.5
    )

    form_component = min(
        form * 0.25,
        2.5
    )

    ppg_component = min(
        ppg * 0.25,
        2.5
    )

    threat_component = min(
        threat / 100,
        2.0
    )

    creativity_component = min(
        creativity / 100,
        1.5
    )

    influence_component = min(
        influence / 100,
        1.5
    )

    raw_score = (
        ep * 0.40
        + form_component
        + ppg_component
        + xgi_component
        + threat_component
        + creativity_component
        + influence_component
    )

    # Apply minutes probability.
    raw_score *= (
        0.65 + (minutes * 0.35)
    )

    # Apply fixture strength.
    raw_score *= fixture_score

    return round(
        raw_score,
        3
    )


def prediction_explanation(
    bootstrap,
    fixtures,
    player
):

    score = player_prediction_score(
        bootstrap,
        fixtures,
        player
    )

    ep = player_expected_points(
        player
    )

    form = safe_float(
        player.get("form")
    )

    ppg = safe_float(
        player.get("points_per_game")
    )

    xgi = safe_float(
        player.get(
            "expected_goal_involvements"
        )
    )

    return {
        "score": score,
        "ep": ep,
        "form": form,
        "ppg": ppg,
        "xgi": xgi
    }


# ============================================================
# STARTING XI
# ============================================================

def optimize_starting_xi(
    bootstrap,
    fixtures,
    user_picks
):

    players_map = {
        p["id"]: p
        for p in bootstrap["elements"]
    }

    squad = []

    for pick in user_picks:

        player = players_map.get(
            pick["element"]
        )

        if not player:
            continue

        prediction = player_prediction_score(
            bootstrap,
            fixtures,
            player,
            gameweeks=1
        )

        squad.append(
            {
                "id": player["id"],
                "name": player_name(player),
                "element_type": player["element_type"],
                "ep": player_expected_points(player),
                "prediction": prediction,
                "multiplier": pick.get(
                    "multiplier",
                    0
                ),
                "is_captain": pick.get(
                    "is_captain",
                    False
                ),
                "is_vice": pick.get(
                    "is_vice",
                    False
                )
            }
        )

    if len(squad) < 15:
        raise ValueError(
            "FPL returned fewer than 15 players."
        )

    problem = pulp.LpProblem(
        "FPL_Starting_XI",
        pulp.LpMaximize
    )

    x = {
        p["id"]: pulp.LpVariable(
            f"starter_{p['id']}",
            cat="Binary"
        )
        for p in squad
    }

    problem += pulp.lpSum(
        p["prediction"] * x[p["id"]]
        for p in squad
    )

    problem += pulp.lpSum(
        x[p["id"]]
        for p in squad
    ) == 11

    problem += pulp.lpSum(
        x[p["id"]]
        for p in squad
        if p["element_type"] == 1
    ) == 1

    problem += pulp.lpSum(
        x[p["id"]]
        for p in squad
        if p["element_type"] == 2
    ) >= 3

    problem += pulp.lpSum(
        x[p["id"]]
        for p in squad
        if p["element_type"] == 2
    ) <= 5

    problem += pulp.lpSum(
        x[p["id"]]
        for p in squad
        if p["element_type"] == 3
    ) >= 2

    problem += pulp.lpSum(
        x[p["id"]]
        for p in squad
        if p["element_type"] == 3
    ) <= 5

    problem += pulp.lpSum(
        x[p["id"]]
        for p in squad
        if p["element_type"] == 4
    ) >= 1

    problem += pulp.lpSum(
        x[p["id"]]
        for p in squad
        if p["element_type"] == 4
    ) <= 3

    status = problem.solve(
        pulp.PULP_CBC_CMD(
            msg=False
        )
    )

    if status != pulp.LpStatusOptimal:
        raise ValueError(
            "Unable to optimize XI."
        )

    starters = [
        p for p in squad
        if x[p["id"]].value() == 1
    ]

    bench = [
        p for p in squad
        if x[p["id"]].value() != 1
    ]

    bench.sort(
        key=lambda p: p["prediction"],
        reverse=True
    )

    captain = max(
        starters,
        key=lambda p: p["prediction"]
    )

    vice = max(
        [
            p for p in starters
            if p["id"] != captain["id"]
        ],
        key=lambda p: p["prediction"]
    )

    return (
        starters,
        bench,
        captain,
        vice
    )


# ============================================================
# FREE HIT
# ============================================================

def optimize_free_hit(
    bootstrap,
    fixtures
):

    players = [
        p for p in bootstrap["elements"]
        if player_fit(p)
    ]

    problem = pulp.LpProblem(
        "FPL_Free_Hit",
        pulp.LpMaximize
    )

    x = {
        p["id"]: pulp.LpVariable(
            f"fh_{p['id']}",
            cat="Binary"
        )
        for p in players
    }

    # Use our composite prediction model.
    problem += pulp.lpSum(
        player_prediction_score(
            bootstrap,
            fixtures,
            p,
            gameweeks=1
        ) * x[p["id"]]
        for p in players
    )

    problem += pulp.lpSum(
        x[p["id"]]
        for p in players
    ) == 15

    problem += pulp.lpSum(
        p["now_cost"] * x[p["id"]]
        for p in players
    ) <= 1000

    problem += pulp.lpSum(
        x[p["id"]]
        for p in players
        if p["element_type"] == 1
    ) == 2

    problem += pulp.lpSum(
        x[p["id"]]
        for p in players
        if p["element_type"] == 2
    ) == 5

    problem += pulp.lpSum(
        x[p["id"]]
        for p in players
        if p["element_type"] == 3
    ) == 5

    problem += pulp.lpSum(
        x[p["id"]]
        for p in players
        if p["element_type"] == 4
    ) == 3

    for team in bootstrap["teams"]:

        problem += pulp.lpSum(
            x[p["id"]]
            for p in players
            if p["team"] == team["id"]
        ) <= 3

    status = problem.solve(
        pulp.PULP_CBC_CMD(
            msg=False
        )
    )

    if status != pulp.LpStatusOptimal:
        return None

    selected = [
        p for p in players
        if x[p["id"]].value() == 1
    ]

    selected.sort(
        key=lambda p: (
            p["element_type"],
            -player_prediction_score(
                bootstrap,
                fixtures,
                p
            )
        )
    )

    return selected


# ============================================================
# TEAM VALIDITY FOR TRANSFERS
# ============================================================

def squad_team_count(
    squad_players
):

    counts = {}

    for player in squad_players:

        team = player["team"]

        counts[team] = (
            counts.get(team, 0)
            + 1
        )

    return counts


def valid_transfer_team(
    squad_players,
    outgoing,
    incoming
):

    new_squad = [
        p for p in squad_players
        if p["id"] != outgoing["id"]
    ]

    new_squad.append(
        incoming
    )

    counts = squad_team_count(
        new_squad
    )

    return all(
        count <= 3
        for count in counts.values()
    )


# ============================================================
# TRANSFER ENGINE
# ============================================================

def find_transfer_recommendations(
    bootstrap,
    fixtures,
    team_id,
    max_results=5
):

    entry = fetch_user_entry(
        team_id
    )

    picks_data, gameweek = fetch_user_picks(
        team_id
    )

    if not entry or not picks_data:
        return [], None, gameweek

    bank = safe_float(
        entry.get("last_deadline_bank", 0)
    ) / 10

    # Fallback.
    if bank < 0:
        bank = 0

    players_map = {
        p["id"]: p
        for p in bootstrap["elements"]
    }

    squad = []

    for pick in picks_data["picks"]:

        player = players_map.get(
            pick["element"]
        )

        if player:
            squad.append(
                player
            )

    recommendations = []

    squad_ids = {
        p["id"]
        for p in squad
    }

    available_budget = bank

    for outgoing in squad:

        out_score = player_prediction_score(
            bootstrap,
            fixtures,
            outgoing,
            gameweeks=2
        )

        for incoming in bootstrap["elements"]:

            if incoming["id"] in squad_ids:
                continue

            if not player_fit(incoming):
                continue

            if (
                incoming["element_type"]
                != outgoing["element_type"]
            ):
                continue

            price_difference = (
                incoming["now_cost"]
                - outgoing["now_cost"]
            ) / 10

            # The incoming player must be affordable
            # using the user's bank.
            if price_difference > available_budget:
                continue

            if not valid_transfer_team(
                squad,
                outgoing,
                incoming
            ):
                continue

            in_score = player_prediction_score(
                bootstrap,
                fixtures,
                incoming,
                gameweeks=2
            )

            gain = (
                in_score
                - out_score
            )

            if gain <= 0:
                continue

            recommendations.append(
                {
                    "out": outgoing,
                    "in": incoming,
                    "out_score": out_score,
                    "in_score": in_score,
                    "gain": gain,
                    "price_difference":
                        price_difference
                }
            )

    recommendations.sort(
        key=lambda x: x["gain"],
        reverse=True
    )

    return (
        recommendations[:max_results],
        bank,
        gameweek
    )


# ============================================================
# HIT ANALYSIS
# ============================================================

def analyze_hits(
    recommendations
):

    results = []

    for recommendation in recommendations:

        gain = recommendation["gain"]

        # We are evaluating two gameweeks because the
        # transfer prediction itself uses a two-GW horizon.
        hit_cost = 4

        net_gain = (
            gain - hit_cost
        )

        if net_gain > 0:
            verdict = "🟢 TAKE"
        elif gain >= 3:
            verdict = "🟡 BORDERLINE"
        else:
            verdict = "🔴 HOLD"

        results.append(
            {
                **recommendation,
                "net_gain": net_gain,
                "verdict": verdict
            }
        )

    return results


# ============================================================
# CHIP ANALYSIS
# ============================================================

def analyze_triple_captain(
    bootstrap,
    fixtures,
    user_picks
):

    players_map = {
        p["id"]: p
        for p in bootstrap["elements"]
    }

    squad = []

    for pick in user_picks:

        player = players_map.get(
            pick["element"]
        )

        if player:
            squad.append(
                player
            )

    candidates = [
        p for p in squad
        if player_fit(p)
    ]

    if not candidates:
        return None

    best = None

    for player in candidates:

        fixtures_next = get_upcoming_fixtures(
            bootstrap,
            fixtures,
            player["team"],
            limit=2
        )

        if not fixtures_next:
            continue

        base_score = player_prediction_score(
            bootstrap,
            fixtures,
            player,
            gameweeks=2
        )

        # DGW receives a bonus because a second fixture
        # increases captaincy ceiling.
        dgw_bonus = (
            1.35
            if len(fixtures_next) >= 2
            else 1.0
        )

        tc_score = (
            base_score
            * dgw_bonus
            * 3
        )

        candidate = {
            "name": player_name(player),
            "score": base_score,
            "projected": round(
                tc_score,
                1
            ),
            "fixtures": " + ".join(
                fixture_label(
                    bootstrap,
                    f,
                    player["team"]
                )
                for f in fixtures_next
            ),
            "is_dgw":
                len(fixtures_next) >= 2
        }

        if (
            best is None
            or tc_score > best["projected"]
        ):
            best = candidate

    return best


def evaluate_bench_boost(
    bootstrap,
    fixtures,
    user_picks
):

    players_map = {
        p["id"]: p
        for p in bootstrap["elements"]
    }

    bench = user_picks[11:15]

    ready = 0
    favorable = 0
    bench_score = 0

    for pick in bench:

        player = players_map.get(
            pick["element"]
        )

        if not player:
            continue

        if not player_fit(player):
            continue

        ready += 1

        score = player_prediction_score(
            bootstrap,
            fixtures,
            player,
            gameweeks=1
        )

        bench_score += score

        upcoming = get_upcoming_fixtures(
            bootstrap,
            fixtures,
            player["team"],
            limit=1
        )

        if upcoming:

            fdr = fixture_difficulty(
                upcoming[0],
                player["team"]
            )

            if fdr <= 3:
                favorable += 1

    if ready == 4 and favorable >= 3:

        return (
            "🟢 **READY**\n"
            f"All 4 bench players available.\n"
            f"{favorable}/4 have favorable fixtures."
        )

    if ready == 4:

        return (
            "🟡 **POSSIBLE**\n"
            "All four bench players are available, "
            "but fixtures are mixed."
        )

    return (
        f"🔴 **HOLD**\n"
        f"Only {ready}/4 bench players appear available."
    )


def analyze_wildcard_window(
    bootstrap,
    fixtures
):

    current_gw = get_current_gameweek(
        bootstrap
    )

    results = []

    for gw in range(
        current_gw,
        current_gw + 7
    ):

        gw_fixtures = [
            f
            for f in fixtures
            if (
                f.get("event") == gw
                and not f.get("finished")
            )
        ]

        if not gw_fixtures:
            continue

        difficulty_values = []

        for f in gw_fixtures:

            difficulty_values.append(
                safe_float(
                    f.get(
                        "team_h_difficulty",
                        3
                    ),
                    3
                )
            )

            difficulty_values.append(
                safe_float(
                    f.get(
                        "team_a_difficulty",
                        3
                    ),
                    3
                )
            )

        average_fdr = (
            sum(difficulty_values)
            / len(difficulty_values)
        )

        results.append(
            {
                "gw": gw,
                "fdr": average_fdr
            }
        )

    if not results:

        return (
            f"GW{current_gw}: insufficient fixture "
            "data."
        )

    best = min(
        results,
        key=lambda x: x["fdr"]
    )

    return (
        f"Best broad fixture window: "
        f"**GW{best['gw']}** "
        f"(average FDR {best['fdr']:.2f})."
    )


# ============================================================
# LIVE SCORE ENGINE
# ============================================================

live_state = {}

live_state_lock = threading.Lock()


def calculate_live_team_score(
    bootstrap,
    picks,
    live_data
):

    live_elements = {
        item["id"]: item
        for item in live_data.get(
            "elements",
            []
        )
    }

    players_map = {
        p["id"]: p
        for p in bootstrap["elements"]
    }

    total = 0
    details = []

    for pick in picks:

        player_id = pick["element"]

        player = players_map.get(
            player_id
        )

        live_player = live_elements.get(
            player_id
        )

        if not player or not live_player:
            continue

        stats = live_player.get(
            "stats",
            {}
        )

        points = int(
            stats.get(
                "total_points",
                0
            )
        )

        multiplier = int(
            pick.get(
                "multiplier",
                0
            )
        )

        contribution = (
            points * multiplier
        )

        total += contribution

        details.append(
            {
                "id": player_id,
                "name": player_name(player),
                "points": points,
                "multiplier": multiplier,
                "contribution":
                    contribution,
                "captain":
                    pick.get(
                        "is_captain",
                        False
                    ),
                "vice":
                    pick.get(
                        "is_vice",
                        False
                    )
            }
        )

    return total, details


def format_live_summary(
    gameweek,
    total,
    details
):

    active = [
        p for p in details
        if p["multiplier"] > 0
    ]

    active.sort(
        key=lambda p: p["points"],
        reverse=True
    )

    lines = []

    for p in active:

        suffix = ""

        if p["captain"]:
            suffix = " (C)"

        elif p["vice"]:
            suffix = " (VC)"

        multiplier = (
            f" x{p['multiplier']}"
            if p["multiplier"] > 1
            else ""
        )

        lines.append(
            f"• {p['name']}{suffix}: "
            f"{p['points']} pts{multiplier}"
        )

    return (
        f"🔴 **LIVE GAMEWEEK {gameweek}**\n\n"
        f"🎯 **Live Team Points:** `{total}`\n\n"
        + "\n".join(lines)
    )


# ============================================================
# AUTOMATIC LIVE MONITOR
# ============================================================

async def monitor_live_scores():

    logger.info(
        "Live monitor started."
    )

    while True:

        try:

            users = get_all_user_teams()

            if not users:

                await asyncio.sleep(
                    LIVE_POLL_SECONDS
                )

                continue

            # ------------------------------------------------
            # IMPORTANT:
            # Download bootstrap + fixtures once.
            # ------------------------------------------------

            bootstrap, fixtures = get_fpl_data(
                force_refresh=True
            )

            if not bootstrap:

                await asyncio.sleep(
                    LIVE_POLL_SECONDS
                )

                continue

            gameweek = get_current_gameweek(
                bootstrap
            )

            # ------------------------------------------------
            # IMPORTANT:
            # Download LIVE data only once.
            # ------------------------------------------------

            live_data = fetch_live_gameweek(
                gameweek
            )

            if not live_data:

                await asyncio.sleep(
                    LIVE_POLL_SECONDS
                )

                continue

            for chat_id, team_id in users:

                try:

                    picks_data, _ = fetch_user_picks(
                        team_id
                    )

                    if not picks_data:
                        continue

                    total, details = (
                        calculate_live_team_score(
                            bootstrap,
                            picks_data["picks"],
                            live_data
                        )
                    )

                    state_key = (
                        f"{chat_id}:"
                        f"{team_id}:"
                        f"{gameweek}"
                    )

                    with live_state_lock:

                        previous = live_state.get(
                            state_key
                        )

                        live_state[state_key] = {
                            "total": total,
                            "details": details
                        }

                    # Establish baseline.
                    if previous is None:
                        continue

                    old_total = previous["total"]

                    if old_total == total:
                        continue

                    difference = (
                        total - old_total
                    )

                    changed = []

                    old_players = {
                        p["id"]: p
                        for p in previous["details"]
                    }

                    for current in details:

                        old = old_players.get(
                            current["id"]
                        )

                        if not old:
                            continue

                        if (
                            old["points"]
                            != current["points"]
                        ):

                            changed.append(
                                f"• {current['name']}: "
                                f"{old['points']} → "
                                f"{current['points']} "
                                f"({current['points'] - old['points']:+d})"
                            )

                    sign = (
                        "+"
                        if difference > 0
                        else ""
                    )

                    message = (
                        f"🔴 **FPL LIVE UPDATE — GW{gameweek}**\n\n"
                        f"🎯 Team points: "
                        f"`{total}` "
                        f"({sign}{difference})\n\n"
                    )

                    if changed:

                        message += (
                            "**Player changes:**\n"
                            + "\n".join(
                                changed[:10]
                            )
                        )

                    else:

                        message += (
                            "Your live team score changed."
                        )

                    await application.bot.send_message(
                        chat_id=chat_id,
                        text=message,
                        parse_mode="Markdown"
                    )

                except Exception as exc:

                    logger.exception(
                        "Live tracking error "
                        "for team %s: %s",
                        team_id,
                        exc
                    )

        except Exception as exc:

            logger.exception(
                "Live monitor error: %s",
                exc
            )

        await asyncio.sleep(
            LIVE_POLL_SECONDS
        )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start_command(
    update,
    context
):

    text = (
        "🤖 **FPL Tactical Assistant**\n\n"
        "Commands:\n\n"
        "• `/setteam <ID>` — Link FPL team\n"
        "• `/squad` — Optimize XI + captain\n"
        "• `/freehit` — Optimize Free Hit squad\n"
        "• `/transfers` — Find transfer upgrades\n"
        "• `/hits` — Analyze -4 options\n"
        "• `/live` — Current live points\n"
        "• `/chips` — Analyze chips\n\n"
        "🔴 Automatic live monitoring is enabled."
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown"
    )


async def setteam_command(
    update,
    context
):

    chat_id = update.effective_chat.id

    if not context.args:

        await update.message.reply_text(
            "Usage:\n"
            "`/setteam 1234567`",
            parse_mode="Markdown"
        )

        return

    try:

        team_id = int(
            context.args[0]
        )

        if team_id <= 0:
            raise ValueError

    except ValueError:

        await update.message.reply_text(
            "❌ Invalid FPL Team ID."
        )

        return

    picks, gameweek = fetch_user_picks(
        team_id
    )

    if not picks:

        await update.message.reply_text(
            "❌ FPL team not found or "
            "could not be retrieved."
        )

        return

    save_team_id(
        chat_id,
        team_id
    )

    await update.message.reply_text(
        f"✅ **Team linked.**\n\n"
        f"Team ID: `{team_id}`\n"
        f"Gameweek: `{gameweek}`\n\n"
        f"🔴 Automatic live monitoring enabled.",
        parse_mode="Markdown"
    )


async def squad_command(
    update,
    context
):

    chat_id = update.effective_chat.id

    team_id = get_team_id(
        chat_id
    )

    if not team_id:

        await update.message.reply_text(
            "⚠️ Use `/setteam <ID>` first.",
            parse_mode="Markdown"
        )

        return

    await update.message.reply_text(
        "⚙️ Running tactical prediction model..."
    )

    try:

        bootstrap, fixtures = get_fpl_data()

        picks_data, gameweek = fetch_user_picks(
            team_id
        )

        if not bootstrap or not fixtures or not picks_data:
            raise ValueError

        starters, bench, captain, vice = (
            await asyncio.to_thread(
                optimize_starting_xi,
                bootstrap,
                fixtures,
                picks_data["picks"]
            )
        )

        by_position = {
            1: [],
            2: [],
            3: [],
            4: []
        }

        for p in starters:

            by_position[
                p["element_type"]
            ].append(p)

        lines = []

        for position in [1, 2, 3, 4]:

            players = by_position[
                position
            ]

            if not players:
                continue

            names = ", ".join(
                f"{p['name']} "
                f"({p['prediction']:.1f})"
                for p in players
            )

            lines.append(
                f"• **{POSITION_NAMES[position]}:** "
                f"{names}"
            )

        bench_text = ", ".join(
            f"{p['name']} "
            f"({p['prediction']:.1f})"
            for p in bench
        )

        response = (
            f"⚽ **OPTIMAL XI — GW{gameweek}**\n\n"
            + "\n".join(lines)
            + "\n\n"
            f"👑 **Captain:** "
            f"{captain['name']}\n"
            f"Model score: "
            f"`{captain['prediction']:.2f}`\n\n"
            f"🛡️ **Vice:** "
            f"{vice['name']}\n"
            f"Model score: "
            f"`{vice['prediction']:.2f}`\n\n"
            f"🪑 **Bench:** "
            f"{bench_text}\n\n"
            "📊 Model combines EP, form, PPG, "
            "expected goal involvement, ICT metrics, "
            "availability and fixture difficulty."
        )

        await update.message.reply_text(
            response,
            parse_mode="Markdown"
        )

    except Exception:

        logger.exception(
            "Squad command failed."
        )

        await update.message.reply_text(
            "❌ Could not optimize your squad."
        )


async def freehit_command(
    update,
    context
):

    await update.message.reply_text(
        "🛠️ Building Free Hit using the tactical prediction model..."
    )

    try:

        bootstrap, fixtures = get_fpl_data()

        if not bootstrap or not fixtures:
            raise ValueError

        squad = await asyncio.to_thread(
            optimize_free_hit,
            bootstrap,
            fixtures
        )

        if not squad:
            raise ValueError

        total_cost = sum(
            p["now_cost"]
            for p in squad
        ) / 10

        by_position = {
            1: [],
            2: [],
            3: [],
            4: []
        }

        for p in squad:

            by_position[
                p["element_type"]
            ].append(p)

        lines = []

        for position in [1, 2, 3, 4]:

            names = ", ".join(
                f"{player_name(p)} "
                f"(£{player_price(p):.1f}, "
                f"{player_prediction_score(bootstrap, fixtures, p):.1f})"
                for p in by_position[position]
            )

            lines.append(
                f"• **{POSITION_NAMES[position]}:** "
                f"{names}"
            )

        response = (
            f"🌟 **FREE HIT — GW{get_current_gameweek(bootstrap)}**\n\n"
            f"💰 Cost: **£{total_cost:.1f}m**\n\n"
            + "\n".join(lines)
            + "\n\n"
            "📊 Ranking uses the composite tactical "
            "prediction model."
        )

        await update.message.reply_text(
            response,
            parse_mode="Markdown"
        )

    except Exception:

        logger.exception(
            "Free Hit failed."
        )

        await update.message.reply_text(
            "❌ Could not calculate a valid Free Hit."
        )


async def transfers_command(
    update,
    context
):

    chat_id = update.effective_chat.id

    team_id = get_team_id(
        chat_id
    )

    if not team_id:

        await update.message.reply_text(
            "⚠️ Link your FPL team first."
        )

        return

    await update.message.reply_text(
        "🔄 Scanning your squad, bank, fixtures and club limits..."
    )

    try:

        bootstrap, fixtures = get_fpl_data()

        recommendations, bank, gameweek = (
            await asyncio.to_thread(
                find_transfer_recommendations,
                bootstrap,
                fixtures,
                team_id
            )
        )

        if not recommendations:

            await update.message.reply_text(
                f"No positive transfer upgrades found.\n\n"
                f"🏦 Estimated bank: £{bank:.1f}m"
            )

            return

        lines = []

        for r in recommendations:

            outgoing = r["out"]
            incoming = r["in"]

            extra = r["price_difference"]

            price_text = (
                f"+£{extra:.1f}m"
                if extra > 0
                else f"-£{abs(extra):.1f}m"
            )

            lines.append(
                f"🔁 **{player_name(outgoing)} "
                f"→ {player_name(incoming)}**\n"
                f"   Price change: {price_text}\n"
                f"   Model: "
                f"{r['out_score']:.2f} → "
                f"{r['in_score']:.2f}\n"
                f"   Projected improvement: "
                f"**+{r['gain']:.2f}**"
            )

        response = (
            f"🔄 **TRANSFER INTELLIGENCE — GW{gameweek}**\n\n"
            f"🏦 Bank available: **£{bank:.1f}m**\n\n"
            + "\n\n".join(lines)
            + "\n\n"
            "Recommendations use a two-GW horizon and "
            "respect the three-player-per-club restriction."
        )

        await update.message.reply_text(
            response,
            parse_mode="Markdown"
        )

    except Exception:

        logger.exception(
            "Transfer command failed."
        )

        await update.message.reply_text(
            "❌ Could not calculate transfers."
        )


async def hits_command(
    update,
    context
):

    chat_id = update.effective_chat.id

    team_id = get_team_id(
        chat_id
    )

    if not team_id:

        await update.message.reply_text(
            "⚠️ Link your FPL team first."
        )

        return

    await update.message.reply_text(
        "⚖️ Calculating expected value of transfer hits..."
    )

    try:

        bootstrap, fixtures = get_fpl_data()

        recommendations, bank, gameweek = (
            await asyncio.to_thread(
                find_transfer_recommendations,
                bootstrap,
                fixtures,
                team_id,
                10
            )
        )

        results = analyze_hits(
            recommendations
        )

        if not results:

            await update.message.reply_text(
                "No transfer currently projects enough "
                "gain to justify a -4."
            )

            return

        lines = []

        for r in results[:5]:

            lines.append(
                f"{r['verdict']} "
                f"**{player_name(r['out'])} → "
                f"{player_name(r['in'])}**\n"
                f"Expected gain: "
                f"+{r['gain']:.2f}\n"
                f"After -4: "
                f"{r['net_gain']:+.2f}"
            )

        response = (
            f"⚖️ **HIT ANALYSIS — GW{gameweek}**\n\n"
            + "\n\n".join(lines)
            + "\n\n"
            "This model uses a two-GW horizon. "
            "It does not guarantee the actual points outcome."
        )

        await update.message.reply_text(
            response,
            parse_mode="Markdown"
        )

    except Exception:

        logger.exception(
            "Hit command failed."
        )

        await update.message.reply_text(
            "❌ Could not calculate hit analysis."
        )


async def live_command(
    update,
    context
):

    chat_id = update.effective_chat.id

    team_id = get_team_id(
        chat_id
    )

    if not team_id:

        await update.message.reply_text(
            "⚠️ Link your FPL team first."
        )

        return

    try:

        bootstrap, _ = get_fpl_data()

        picks_data, gameweek = fetch_user_picks(
            team_id
        )

        if not bootstrap or not picks_data:
            raise ValueError

        live_data = fetch_live_gameweek(
            gameweek
        )

        if not live_data:
            raise ValueError

        total, details = (
            calculate_live_team_score(
                bootstrap,
                picks_data["picks"],
                live_data
            )
        )

        response = format_live_summary(
            gameweek,
            total,
            details
        )

        await update.message.reply_text(
            response,
            parse_mode="Markdown"
        )

    except Exception:

        logger.exception(
            "Live command failed."
        )

        await update.message.reply_text(
            "❌ Live FPL data unavailable."
        )


async def chips_command(
    update,
    context
):

    chat_id = update.effective_chat.id

    team_id = get_team_id(
        chat_id
    )

    if not team_id:

        await update.message.reply_text(
            "⚠️ Link your FPL team first."
        )

        return

    await update.message.reply_text(
        "📊 Running chip analysis..."
    )

    try:

        bootstrap, fixtures = get_fpl_data()

        picks_data, gameweek = fetch_user_picks(
            team_id
        )

        if not bootstrap or not fixtures or not picks_data:
            raise ValueError

        picks = picks_data["picks"]

        tc = analyze_triple_captain(
            bootstrap,
            fixtures,
            picks
        )

        bb = evaluate_bench_boost(
            bootstrap,
            fixtures,
            picks
        )

        wc = analyze_wildcard_window(
            bootstrap,
            fixtures
        )

        if tc:

            tc_text = (
                f"👑 **Triple Captain:** "
                f"{tc['name']}\n"
                f"Fixtures: {tc['fixtures']}\n"
                f"Model TC ceiling: "
                f"{tc['projected']} pts\n"
                f"DGW: "
                f"{'Yes 🔥' if tc['is_dgw'] else 'No'}"
            )

        else:

            tc_text = (
                "👑 **Triple Captain:** "
                "No suitable candidate."
            )

        response = (
            f"📊 **CHIP INTELLIGENCE — GW{gameweek}**\n\n"
            f"{tc_text}\n\n"
            f"🪑 **Bench Boost:**\n"
            f"{bb}\n\n"
            f"🔄 **Wildcard:**\n"
            f"{wc}"
        )

        await update.message.reply_text(
            response,
            parse_mode="Markdown"
        )

    except Exception:

        logger.exception(
            "Chip command failed."
        )

        await update.message.reply_text(
            "❌ Could not analyze chips."
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    logger.exception(
        "Telegram error: %s",
        context.error
    )


# ============================================================
# HANDLERS
# ============================================================

application.add_handler(
    CommandHandler(
        "start",
        start_command
    )
)

application.add_handler(
    CommandHandler(
        "setteam",
        setteam_command
    )
)

application.add_handler(
    CommandHandler(
        "squad",
        squad_command
    )
)

application.add_handler(
    CommandHandler(
        "freehit",
        freehit_command
    )
)

application.add_handler(
    CommandHandler(
        "transfers",
        transfers_command
    )
)

application.add_handler(
    CommandHandler(
        "hits",
        hits_command
    )
)

application.add_handler(
    CommandHandler(
        "live",
        live_command
    )
)

application.add_handler(
    CommandHandler(
        "chips",
        chips_command
    )
)

application.add_error_handler(
    error_handler
)


# ============================================================
# TELEGRAM EVENT LOOP
# ============================================================

telegram_loop = None
telegram_thread = None
telegram_ready = threading.Event()


async def initialize_telegram():

    logger.info(
        "Starting Telegram application..."
    )

    await application.initialize()

    await application.start()

    webhook = WEBHOOK_URL.rstrip("/")

    if not webhook.endswith("/webhook"):
        webhook += "/webhook"

    webhook_kwargs = {
        "url": webhook,
        "allowed_updates": Update.ALL_TYPES,
        "drop_pending_updates": True
    }

    if WEBHOOK_SECRET:

        webhook_kwargs[
            "secret_token"
        ] = WEBHOOK_SECRET

    await application.bot.set_webhook(
        **webhook_kwargs
    )

    logger.info(
        "Webhook configured: %s",
        webhook
    )

    application.create_task(
        monitor_live_scores()
    )

    telegram_ready.set()

    logger.info(
        "Telegram application ready."
    )


def telegram_event_loop():

    global telegram_loop

    telegram_loop = asyncio.new_event_loop()

    asyncio.set_event_loop(
        telegram_loop
    )

    try:

        telegram_loop.run_until_complete(
            initialize_telegram()
        )

        telegram_loop.run_forever()

    except Exception:

        logger.exception(
            "Telegram event loop crashed."
        )

    finally:

        try:
            telegram_loop.close()
        except Exception:
            pass


def start_telegram_thread():

    global telegram_thread

    if (
        telegram_thread
        and telegram_thread.is_alive()
    ):
        return

    telegram_thread = threading.Thread(
        target=telegram_event_loop,
        daemon=True,
        name="telegram-event-loop"
    )

    telegram_thread.start()

    telegram_ready.wait(
        timeout=30
    )


start_telegram_thread()


# ============================================================
# WEBHOOK
# ============================================================

@app.route(
    "/webhook",
    methods=["POST"]
)
def webhook():

    if WEBHOOK_SECRET:

        incoming_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )

        if incoming_secret != WEBHOOK_SECRET:

            return jsonify(
                {
                    "ok": False,
                    "error": "Unauthorized"
                }
            ), 403

    if not telegram_loop:

        return jsonify(
            {
                "ok": False,
                "error":
                    "Telegram not ready"
            }
        ), 503

    try:

        json_data = request.get_json(
            force=True
        )

        update = Update.de_json(
            json_data,
            application.bot
        )

        asyncio.run_coroutine_threadsafe(
            application.process_update(
                update
            ),
            telegram_loop
        )

        return jsonify(
            {
                "ok": True
            }
        )

    except Exception:

        logger.exception(
            "Webhook error."
        )

        return jsonify(
            {
                "ok": False
            }
        ), 500


# ============================================================
# HEALTH
# ============================================================

@app.route(
    "/health",
    methods=["GET"]
)
def health():

    return jsonify(
        {
            "status": "ok",
            "telegram_ready":
                telegram_ready.is_set(),
            "live_poll_seconds":
                LIVE_POLL_SECONDS
        }
    )


@app.route(
    "/",
    methods=["GET"]
)
def home():

    return jsonify(
        {
            "service":
                "FPL Tactical Assistant",
            "status":
                "running"
        }
    )


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False
    )