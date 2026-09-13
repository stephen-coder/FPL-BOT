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
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

WEBHOOK_URL = os.getenv(
    "WEBHOOK_URL",
    "https://your-render-app.onrender.com/webhook"
)

WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    ""
)

PORT = int(os.getenv("PORT", "5000"))

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
# TELEGRAM APPLICATION
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

ptb_loop = None
ptb_thread = None


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

    try:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_teams (
                chat_id INTEGER PRIMARY KEY,
                team_id INTEGER NOT NULL
            )
        """)

        conn.commit()

    finally:
        conn.close()


def save_team_id(chat_id, team_id):

    conn = get_db()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO user_teams(chat_id, team_id)
            VALUES (?, ?)
            ON CONFLICT(chat_id)
            DO UPDATE SET team_id = excluded.team_id
        """, (chat_id, team_id))

        conn.commit()

    finally:
        conn.close()


def get_team_id(chat_id):

    conn = get_db()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT team_id
            FROM user_teams
            WHERE chat_id = ?
        """, (chat_id,))

        row = cursor.fetchone()

        return row[0] if row else None

    finally:
        conn.close()


def get_all_user_teams():

    conn = get_db()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT chat_id, team_id
            FROM user_teams
        """)

        return cursor.fetchall()

    finally:
        conn.close()


init_db()


# ============================================================
# FPL API
# ============================================================

FPL_BASE = "https://fantasy.premierleague.com/api"

_cache = {
    "bootstrap": None,
    "fixtures": None,
    "timestamp": 0,
}

_cache_lock = threading.Lock()


def fpl_get(endpoint):

    url = f"{FPL_BASE}/{endpoint.lstrip('/')}"

    try:

        response = requests.get(
            url,
            timeout=20,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(compatible; FPL-Tactical-Assistant/3.0)"
                )
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

        # During an off-week, try the next gameweek.
        events = bootstrap.get(
            "events",
            []
        )

        next_gw = next(
            (
                gw["id"]
                for gw in events
                if gw.get("is_next")
            ),
            gameweek
        )

        if next_gw != gameweek:

            picks = fpl_get(
                f"/entry/{team_id}/event/{next_gw}/picks/"
            )

            if picks:
                return picks, next_gw

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
    4: "FWD",
}


def player_name(player):

    return player.get(
        "web_name",
        "Unknown"
    )


def player_price(player):

    return safe_float(
        player.get("now_cost"),
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

    minutes = safe_float(
        player.get("minutes"),
        0
    )

    starts = safe_float(
        player.get("starts"),
        0
    )

    if minutes <= 0:
        return 0.75

    expected_90s = max(
        minutes / 90,
        1
    )

    start_rate = (
        starts / expected_90s
    )

    return max(
        0.55,
        min(
            start_rate,
            1.0
        )
    )


def get_team_name(
    bootstrap,
    team_id
):

    team = next(
        (
            t
            for t in bootstrap.get("teams", [])
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

        opponent_id = fixture.get(
            "team_a"
        )

    else:

        opponent_id = fixture.get(
            "team_h"
        )

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
            fixture.get("team_h") != team_id
            and
            fixture.get("team_a") != team_id
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
# PREDICTION ENGINE
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

        difficulty_factor = (
            1.25
            - ((fdr - 1) * 0.12)
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

    raw_score *= (
        0.65
        + (minutes * 0.35)
    )

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

    return {
        "score": player_prediction_score(
            bootstrap,
            fixtures,
            player
        ),
        "ep": player_expected_points(
            player
        ),
        "form": safe_float(
            player.get("form")
        ),
        "ppg": safe_float(
            player.get("points_per_game")
        ),
        "xgi": safe_float(
            player.get(
                "expected_goal_involvements"
            )
        ),
    }


# ============================================================
# STARTING XI OPTIMIZER
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
            player
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
                ),
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

    # Goalkeeper
    problem += pulp.lpSum(
        x[p["id"]]
        for p in squad
        if p["element_type"] == 1
    ) == 1

    # Defenders
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

    # Midfielders
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

    # Forwards
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
        p
        for p in squad
        if x[p["id"]].value() == 1
    ]

    bench = [
        p
        for p in squad
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

    vice_candidates = [
        p
        for p in starters
        if p["id"] != captain["id"]
    ]

    vice = max(
        vice_candidates,
        key=lambda p: p["prediction"]
    )

    return (
        starters,
        bench,
        captain,
        vice
    )


# ============================================================
# FREE HIT OPTIMIZER
# ============================================================

def optimize_free_hit(
    bootstrap,
    fixtures
):

    players = [
        p
        for p in bootstrap["elements"]
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

    problem += pulp.lpSum(
        player_prediction_score(
            bootstrap,
            fixtures,
            p
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

    # Maximum 3 players from one club.
    for team in bootstrap.get("teams", []):

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
        p
        for p in players
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
# TRANSFER VALIDATION
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
        p
        for p in squad_players
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
# TRANSFER RECOMMENDATIONS
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

    bank = (
        safe_float(
            entry.get(
                "last_deadline_bank",
                0
            )
        ) / 10
    )

    if bank < 0:
        bank = 0

    players_map = {
        p["id"]: p
        for p in bootstrap["elements"]
    }

    squad = []

    for pick in picks_data.get(
        "picks",
        []
    ):

        player = players_map.get(
            pick["element"]
        )

        if player:

            squad.append(
                player
            )

    if len(squad) != 15:

        return [], None, gameweek

    squad_ids = {
        p["id"]
        for p in squad
    }

    recommendations = []

    for outgoing in squad:

        outgoing_score = player_prediction_score(
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

            if price_difference > bank:
                continue

            if not valid_transfer_team(
                squad,
                outgoing,
                incoming
            ):
                continue

            incoming_score = player_prediction_score(
                bootstrap,
                fixtures,
                incoming,
                gameweeks=2
            )

            gain = (
                incoming_score
                - outgoing_score
            )

            if gain <= 0:
                continue

            recommendations.append(
                {
                    "out": outgoing,
                    "in": incoming,
                    "out_score": outgoing_score,
                    "in_score": incoming_score,
                    "gain": gain,
                    "price_difference": price_difference,
                }
            )

    recommendations.sort(
        key=lambda r: r["gain"],
        reverse=True
    )

    return (
        recommendations[:max_results],
        entry,
        gameweek
    )


# ============================================================
# FORMATTING
# ============================================================

def format_player(
    player,
    bootstrap=None
):

    position = POSITION_NAMES.get(
        player.get("element_type"),
        "?"
    )

    price = player_price(
        player
    )

    return (
        f"{player_name(player)} "
        f"({position}, £{price:.1f}m)"
    )


def format_prediction_player(
    player,
    bootstrap,
    fixtures
):

    score = player_prediction_score(
        bootstrap,
        fixtures,
        player
    )

    ep = player_expected_points(
        player
    )

    return (
        f"{player_name(player)} "
        f"— prediction {score:.2f} "
        f"| EP {ep:.1f}"
    )


def format_fixture_list(
    bootstrap,
    fixtures,
    team_id,
    limit=3
):

    upcoming = get_upcoming_fixtures(
        bootstrap,
        fixtures,
        team_id,
        limit=limit
    )

    if not upcoming:
        return "No upcoming fixtures found."

    return ", ".join(
        fixture_label(
            bootstrap,
            fixture,
            team_id
        )
        for fixture in upcoming
    )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.effective_message

    if not message:
        return

    await message.reply_text(
        "⚽ FPL Tactical Assistant\n\n"
        "Commands:\n"
        "/team YOUR_FPL_TEAM_ID - save your team\n"
        "/myteam - show saved team\n"
        "/xi - optimize your starting XI\n"
        "/freehit - build a Free Hit squad\n"
        "/transfers - find transfer targets\n"
        "/help - show commands\n\n"
        "Example:\n"
        "/team 1234567"
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.effective_message.reply_text(
        "⚽ FPL Tactical Assistant\n\n"
        "/team ID — save your FPL team ID\n"
        "/myteam — show saved team ID\n"
        "/xi — optimize your starting XI\n"
        "/freehit — optimize a Free Hit squad\n"
        "/transfers — find transfer recommendations\n"
        "/help — show this message"
    )


async def team_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.effective_message

    if not message:
        return

    if not context.args:

        await message.reply_text(
            "Please provide your FPL team ID.\n\n"
            "Example:\n"
            "/team 1234567"
        )

        return

    try:

        team_id = int(
            context.args[0]
        )

        if team_id <= 0:

            raise ValueError

    except ValueError:

        await message.reply_text(
            "❌ Invalid team ID.\n"
            "Use numbers only.\n\n"
            "Example: /team 1234567"
        )

        return

    entry = fetch_user_entry(
        team_id
    )

    if not entry:

        await message.reply_text(
            "❌ I couldn't find that FPL team.\n"
            "Check your team ID and try again."
        )

        return

    chat_id = update.effective_chat.id

    save_team_id(
        chat_id,
        team_id
    )

    manager = entry.get(
        "player_name",
        "Unknown"
    )

    team_name = entry.get(
        "name",
        "Unnamed Team"
    )

    await message.reply_text(
        "✅ FPL team saved.\n\n"
        f"Team: {team_name}\n"
        f"Manager: {manager}\n"
        f"Team ID: {team_id}\n\n"
        "Use /xi to optimize your XI."
    )


async def myteam_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    team_id = get_team_id(
        chat_id
    )

    if not team_id:

        await update.effective_message.reply_text(
            "You haven't saved an FPL team yet.\n\n"
            "Use:\n"
            "/team YOUR_TEAM_ID"
        )

        return

    entry = fetch_user_entry(
        team_id
    )

    if not entry:

        await update.effective_message.reply_text(
            f"Saved team ID: {team_id}\n"
            "However, FPL could not be reached right now."
        )

        return

    await update.effective_message.reply_text(
        "⚽ Your FPL team\n\n"
        f"Team: {entry.get('name', 'Unknown')}\n"
        f"Manager: {entry.get('player_name', 'Unknown')}\n"
        f"Team ID: {team_id}\n"
        f"Overall rank: "
        f"{entry.get('summary_overall_rank', 'N/A')}"
    )


async def xi_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.effective_message

    chat_id = update.effective_chat.id

    team_id = get_team_id(
        chat_id
    )

    if not team_id:

        await message.reply_text(
            "❌ Save your FPL team first.\n\n"
            "Use:\n"
            "/team YOUR_TEAM_ID"
        )

        return

    await message.reply_text(
        "🔎 Analyzing your squad..."
    )

    bootstrap, fixtures = get_fpl_data(
        force_refresh=True
    )

    if not bootstrap or not fixtures:

        await message.reply_text(
            "❌ FPL data is temporarily unavailable."
        )

        return

    picks_data, gameweek = fetch_user_picks(
        team_id
    )

    if not picks_data:

        await message.reply_text(
            "❌ I couldn't retrieve your squad."
        )

        return

    try:

        starters, bench, captain, vice = (
            optimize_starting_xi(
                bootstrap,
                fixtures,
                picks_data["picks"]
            )
        )

    except Exception as exc:

        logger.exception(
            "XI optimization failed"
        )

        await message.reply_text(
            f"❌ XI optimization failed:\n{exc}"
        )

        return

    position_order = {
        1: 0,
        2: 1,
        3: 2,
        4: 3,
    }

    starters.sort(
        key=lambda p: (
            position_order.get(
                p["element_type"],
                9
            ),
            -p["prediction"]
        )
    )

    lines = [
        f"⚽ OPTIMIZED XI — GW{gameweek}",
        "",
    ]

    for player in starters:

        marker = ""

        if player["id"] == captain["id"]:
            marker = " ©"

        elif player["id"] == vice["id"]:
            marker = " (VC)"

        lines.append(
            f"• {player['name']}{marker} "
            f"[{POSITION_NAMES.get(player['element_type'], '?')}] "
            f"{player['prediction']:.2f}"
        )

    lines.extend(
        [
            "",
            f"© Captain: {captain['name']}",
            f"VC: {vice['name']}",
            "",
            "🪑 BENCH",
        ]
    )

    for index, player in enumerate(
        bench,
        start=1
    ):

        lines.append(
            f"{index}. {player['name']} "
            f"[{POSITION_NAMES.get(player['element_type'], '?')}] "
            f"{player['prediction']:.2f}"
        )

    await message.reply_text(
        "\n".join(lines)
    )


async def freehit_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.effective_message

    await message.reply_text(
        "🔎 Building the best Free Hit squad..."
    )

    bootstrap, fixtures = get_fpl_data(
        force_refresh=True
    )

    if not bootstrap or not fixtures:

        await message.reply_text(
            "❌ FPL data is temporarily unavailable."
        )

        return

    try:

        selected = optimize_free_hit(
            bootstrap,
            fixtures
        )

    except Exception as exc:

        logger.exception(
            "Free Hit optimization failed"
        )

        await message.reply_text(
            f"❌ Free Hit optimization failed:\n{exc}"
        )

        return

    if not selected:

        await message.reply_text(
            "❌ Unable to create a Free Hit squad."
        )

        return

    total_cost = sum(
        p["now_cost"]
        for p in selected
    ) / 10

    lines = [
        "🔥 FREE HIT SQUAD",
        "",
        f"Budget used: £{total_cost:.1f}m",
        "",
    ]

    for position in [1, 2, 3, 4]:

        position_players = [
            p
            for p in selected
            if p["element_type"] == position
        ]

        if not position_players:
            continue

        lines.append(
            POSITION_NAMES[position] + ":"
        )

        for player in position_players:

            prediction = player_prediction_score(
                bootstrap,
                fixtures,
                player
            )

            lines.append(
                f"• {player_name(player)} "
                f"— £{player_price(player):.1f}m "
                f"— {prediction:.2f}"
            )

        lines.append("")

    await message.reply_text(
        "\n".join(lines)
    )


async def transfers_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.effective_message

    chat_id = update.effective_chat.id

    team_id = get_team_id(
        chat_id
    )

    if not team_id:

        await message.reply_text(
            "❌ Save your FPL team first.\n\n"
            "Use:\n"
            "/team YOUR_TEAM_ID"
        )

        return

    await message.reply_text(
        "🔎 Searching for transfer improvements..."
    )

    bootstrap, fixtures = get_fpl_data(
        force_refresh=True
    )

    if not bootstrap or not fixtures:

        await message.reply_text(
            "❌ FPL data is temporarily unavailable."
        )

        return

    try:

        recommendations, entry, gameweek = (
            find_transfer_recommendations(
                bootstrap,
                fixtures,
                team_id,
                max_results=5
            )
        )

    except Exception as exc:

        logger.exception(
            "Transfer engine failed"
        )

        await message.reply_text(
            f"❌ Transfer analysis failed:\n{exc}"
        )

        return

    if not recommendations:

        await message.reply_text(
            "No positive transfer improvements "
            "were found with the current squad/budget."
        )

        return

    lines = [
        f"🔄 TRANSFER TARGETS — GW{gameweek}",
        "",
    ]

    for index, rec in enumerate(
        recommendations,
        start=1
    ):

        outgoing = rec["out"]
        incoming = rec["in"]

        difference = rec[
            "price_difference"
        ]

        if difference >= 0:

            price_text = (
                f"+£{difference:.1f}m"
            )

        else:

            price_text = (
                f"-£{abs(difference):.1f}m"
            )

        lines.extend(
            [
                f"{index}. "
                f"{player_name(outgoing)} "
                f"➡️ "
                f"{player_name(incoming)}",
                f"   Projection: "
                f"{rec['out_score']:.2f} "
                f"➡️ {rec['in_score']:.2f}",
                f"   Gain: +{rec['gain']:.2f}",
                f"   Price: {price_text}",
                "",
            ]
        )

    await message.reply_text(
        "\n".join(lines)
    )


# ============================================================
# REGISTER COMMANDS
# ============================================================

application.add_handler(
    CommandHandler(
        "start",
        start_command
    )
)

application.add_handler(
    CommandHandler(
        "help",
        help_command
    )
)

application.add_handler(
    CommandHandler(
        "team",
        team_command
    )
)

application.add_handler(
    CommandHandler(
        "myteam",
        myteam_command
    )
)

application.add_handler(
    CommandHandler(
        "xi",
        xi_command
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


# ============================================================
# TELEGRAM BACKGROUND LOOP
# ============================================================

async def telegram_main():

    global ptb_loop

    ptb_loop = asyncio.get_running_loop()

    logger.info(
        "Initializing Telegram application..."
    )

    await application.initialize()

    await application.start()

    # Set Telegram webhook.
    webhook_url = WEBHOOK_URL.rstrip(
        "/"
    )

    if webhook_url:

        try:

            await application.bot.set_webhook(
                url=webhook_url,
                secret_token=(
                    WEBHOOK_SECRET
                    if WEBHOOK_SECRET
                    else None
                ),
                allowed_updates=[
                    "message",
                    "callback_query",
                ],
            )

            logger.info(
                "Telegram webhook configured: %s",
                webhook_url
            )

        except Exception as exc:

            logger.exception(
                "Unable to configure Telegram webhook: %s",
                exc
            )

    logger.info(
        "Telegram application started."
    )

    # Keep the asyncio loop alive.
    await asyncio.Event().wait()


def run_telegram_loop():

    global ptb_loop

    try:

        asyncio.run(
            telegram_main()
        )

    except Exception:

        logger.exception(
            "Telegram background loop stopped."
        )


def start_telegram_thread():

    global ptb_thread

    if ptb_thread is not None:
        return

    ptb_thread = threading.Thread(
        target=run_telegram_loop,
        daemon=True
    )

    ptb_thread.start()

    # Give the Telegram loop a moment to initialize.
    time.sleep(2)


# ============================================================
# FLASK ROUTES
# ============================================================

@app.get("/")
def home():

    return jsonify(
        {
            "status": "online",
            "service": "FPL Tactical Assistant",
        }
    )


@app.get("/health")
def health():

    return jsonify(
        {
            "status": "healthy",
            "telegram": bool(TOKEN),
        }
    )


@app.post("/webhook")
def telegram_webhook():

    global ptb_loop

    # Validate Telegram secret when configured.
    if WEBHOOK_SECRET:

        supplied_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        )

        if supplied_secret != WEBHOOK_SECRET:

            logger.warning(
                "Rejected webhook request with invalid secret."
            )

            return jsonify(
                {
                    "ok": False,
                    "error": "Unauthorized",
                }
            ), 403

    if ptb_loop is None:

        logger.error(
            "Telegram event loop is not ready."
        )

        return jsonify(
            {
                "ok": False,
                "error": "Telegram not ready",
            }
        ), 503

    try:

        data = request.get_json(
            force=True
        )

        update = Update.de_json(
            data,
            application.bot
        )

        future = asyncio.run_coroutine_threadsafe(
            application.update_queue.put(
                update
            ),
            ptb_loop
        )

        future.result(
            timeout=5
        )

        return jsonify(
            {
                "ok": True
            }
        )

    except Exception as exc:

        logger.exception(
            "Webhook processing failed: %s",
            exc
        )

        return jsonify(
            {
                "ok": False,
                "error": str(exc),
            }
        ), 500


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(404)
def not_found(error):

    return jsonify(
        {
            "ok": False,
            "error": "Not found",
        }
    ), 404


@app.errorhandler(500)
def internal_error(error):

    logger.exception(
        "Internal server error: %s",
        error
    )

    return jsonify(
        {
            "ok": False,
            "error": "Internal server error",
        }
    ), 500


# ============================================================
# STARTUP
# ============================================================

def startup():

    logger.info(
        "Starting FPL Tactical Assistant..."
    )

    if not TOKEN:

        logger.warning(
            "TELEGRAM_BOT_TOKEN is missing. "
            "Telegram functionality will not work."
        )

        return

    start_telegram_thread()


# ============================================================
# MAIN
# ============================================================

startup()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False
    )