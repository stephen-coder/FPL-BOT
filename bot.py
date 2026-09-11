import os
import json
import threading
import asyncio
import pulp
import requests
from flask import Flask, request
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# --- SIMPLE PERSISTENCE FOR LINKED TEAM IDS ---
# Render's filesystem is ephemeral across deploys, but this survives
# ordinary restarts/reboots within the same container instance, which
# in-memory user_data does not.

DATA_FILE = os.getenv("FPL_BOT_DATA_FILE", "user_teams.json")
_data_lock = threading.Lock()


def _load_team_map():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_team_map(mapping):
    with _data_lock:
        with open(DATA_FILE, "w") as f:
            json.dump(mapping, f)


def get_team_id(chat_id):
    return _load_team_map().get(str(chat_id))


def set_team_id(chat_id, team_id):
    mapping = _load_team_map()
    mapping[str(chat_id)] = team_id
    _save_team_map(mapping)


# --- FPL DATA FETCHERS ---

FPL_BASE_URL = "https://fantasy.premierleague.com/api/"

# The FPL API blocks requests that don't look like they came from a
# browser (returns 403). A realistic User-Agent (plus a couple of the
# headers a real browser sends) fixes the /squad 403s.
FPL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://fantasy.premierleague.com/",
}


def get_fpl_bootstrap():
    """Fetches core database metrics from the official FPL API."""
    url = f"{FPL_BASE_URL}bootstrap-static/"
    res = requests.get(url, headers=FPL_HEADERS, timeout=15)
    res.raise_for_status()
    return res.json()


def get_next_gw(data):
    """Finds the upcoming active Gameweek ID."""
    for gw in data["events"]:
        if gw["is_next"]:
            return gw["id"]
    return 1


def fetch_user_squad(team_id):
    """Pulls a user's current 15-man squad, remaining bank balance, and team entry info."""
    data = get_fpl_bootstrap()
    next_gw = get_next_gw(data)
    prev_gw = max(1, next_gw - 1)

    url = f"{FPL_BASE_URL}entry/{team_id}/event/{prev_gw}/picks/"
    try:
        res = requests.get(url, headers=FPL_HEADERS, timeout=15)
    except requests.RequestException:
        return None, 0.0, next_gw, {}

    if res.status_code == 403:
        print(f"FPL API returned 403 for team {team_id} — likely blocked/rate-limited request")
        return None, 0.0, next_gw, {}

    if res.status_code != 200:
        return None, 0.0, next_gw, {}

    picks_data = res.json()
    picks = [p["element"] for p in picks_data["picks"]]
    bank = picks_data.get("entry_history", {}).get("bank", 0) / 10.0

    # Also fetch general entry details for /stats
    entry_url = f"{FPL_BASE_URL}entry/{team_id}/"
    entry_res = requests.get(entry_url, headers=FPL_HEADERS, timeout=15)
    entry_info = entry_res.json() if entry_res.status_code == 200 else {}

    types = {t["id"]: t["singular_name_short"] for t in data["element_types"]}
    players_by_id = {p["id"]: p for p in data["elements"]}

    squad = []
    for pid in picks:
        if pid in players_by_id:
            p = players_by_id[pid]
            squad.append(
                {
                    "id": p["id"],
                    "name": p["web_name"],
                    "pos": types[p["element_type"]],
                    "pos_id": p["element_type"],
                    "ep_next": (
                        float(p["ep_next"]) if p["ep_next"] is not None else 0.0
                    ),
                    "cost": p["now_cost"] / 10.0,
                    "team": p["team"],
                }
            )
    return squad, bank, next_gw, entry_info


# --- PULP OPTIMIZATION ENGINES ---


def solve_starting_xi(squad, chip=None):
    """Solves for Starting 11, Captain (C), Vice Captain (VC), and Bench order.
    
    If chip == 'benchboost', all 15 squad members contribute to the total expected points.
    If chip == 'triplecaptain', the captain's points are multiplied by 3.
    """
    if not squad:
        return [], []

    prob = pulp.LpProblem("Lineup_Opt", pulp.LpMaximize)

    start_vars = {
        p["id"]: pulp.LpVariable(f"start_{p['id']}", cat="Binary")
        for p in squad
    }
    cap_vars = {
        p["id"]: pulp.LpVariable(f"cap_{p['id']}", cat="Binary") for p in squad
    }

    # Objective function depends on chips
    cap_multiplier = 3 if chip == 'triplecaptain' else 2
    
    if chip == 'benchboost':
        # All 15 players score points, plus captain multiplier bonus
        prob += pulp.lpSum([p["ep_next"] for p in squad]) + pulp.lpSum(
            [(cap_multiplier - 1) * p["ep_next"] * cap_vars[p["id"]] for p in squad]
        )
        # Bench Boost forces all 15 to start conceptually for scoring
        prob += pulp.lpSum([start_vars[p["id"]] for p in squad]) == 15
    else:
        prob += pulp.lpSum(
            [p["ep_next"] * start_vars[p["id"]] for p in squad]
        ) + pulp.lpSum([(cap_multiplier - 1) * p["ep_next"] * cap_vars[p["id"]] for p in squad])
        prob += (
            pulp.lpSum([start_vars[p["id"]] for p in squad]) == 11
        ), "Exactly_11_Starters"

    prob += (
        pulp.lpSum([cap_vars[p["id"]] for p in squad]) == 1
    ), "Exactly_1_Captain"

    for p in squad:
        prob += cap_vars[p["id"]] <= start_vars[p["id"]]

    gkps = [p for p in squad if p["pos"] == "GKP"]
    defs = [p for p in squad if p["pos"] == "DEF"]
    mids = [p for p in squad if p["pos"] == "MID"]
    fwds = [p for p in squad if p["pos"] == "FWD"]

    prob += pulp.lpSum([start_vars[p["id"]] for p in gkps]) == (2 if chip == 'benchboost' else 1)
    prob += pulp.lpSum([start_vars[p["id"]] for p in defs]) >= (3 if chip != 'benchboost' else 3)
    prob += pulp.lpSum([start_vars[p["id"]] for p in defs]) <= (5 if chip != 'benchboost' else 5)
    prob += pulp.lpSum([start_vars[p["id"]] for p in mids]) >= (2 if chip != 'benchboost' else 2)
    prob += pulp.lpSum([start_vars[p["id"]] for p in mids]) <= (5 if chip != 'benchboost' else 5)
    prob += pulp.lpSum([start_vars[p["id"]] for p in fwds]) >= (1 if chip != 'benchboost' else 1)
    prob += pulp.lpSum([start_vars[p["id"]] for p in fwds]) <= (3 if chip != 'benchboost' else 3)

    status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[status] != "Optimal":
        return [], []

    starters, bench = [], []
    for p in squad:
        p["is_captain"] = pulp.value(cap_vars[p["id"]]) == 1
        if pulp.value(start_vars[p["id"]]) == 1:
            starters.append(p)
        else:
            bench.append(p)

    bench_gkp = [p for p in bench if p["pos"] == "GKP"]
    bench_outfield = sorted(
        [p for p in bench if p["pos"] != "GKP"],
        key=lambda x: x["ep_next"],
        reverse=True,
    )

    non_cap_starters = sorted(
        [p for p in starters if not p["is_captain"]],
        key=lambda x: x["ep_next"],
        reverse=True,
    )
    vc_id = non_cap_starters[0]["id"] if non_cap_starters else None
    for p in starters:
        p["is_vice"] = p["id"] == vc_id

    return starters, bench_gkp + bench_outfield


def solve_transfers(squad, bank, num_transfers=1):
    """
    Jointly optimizes which `num_transfers` players to sell and which to buy,
    under one shared budget, maximizing total expected-points gain.
    """
    if not squad:
        return [], bank, 0.0

    data = get_fpl_bootstrap()
    types = {t["id"]: t["singular_name_short"] for t in data["element_types"]}

    current_ids = {p["id"] for p in squad}
    candidates = []
    for p in data["elements"]:
        if p["id"] in current_ids:
            continue
        if p["status"] == "a" and float(p["ep_next"] or 0) > 0:
            candidates.append(
                {
                    "id": p["id"],
                    "name": p["web_name"],
                    "pos": types[p["element_type"]],
                    "pos_id": p["element_type"],
                    "cost": p["now_cost"] / 10.0,
                    "team": p["team"],
                    "ep_next": float(p["ep_next"] or 0),
                }
            )

    prob = pulp.LpProblem("Transfer_Opt", pulp.LpMaximize)

    out_vars = {p["id"]: pulp.LpVariable(f"out_{p['id']}", cat="Binary") for p in squad}
    in_vars = {c["id"]: pulp.LpVariable(f"in_{c['id']}", cat="Binary") for c in candidates}

    prob += pulp.lpSum([c["ep_next"] * in_vars[c["id"]] for c in candidates]) - pulp.lpSum(
        [p["ep_next"] * out_vars[p["id"]] for p in squad]
    )

    prob += pulp.lpSum(out_vars.values()) == num_transfers
    prob += pulp.lpSum(in_vars.values()) == num_transfers

    for pos_id in {p["pos_id"] for p in squad}:
        prob += pulp.lpSum(
            [out_vars[p["id"]] for p in squad if p["pos_id"] == pos_id]
        ) == pulp.lpSum(
            [in_vars[c["id"]] for c in candidates if c["pos_id"] == pos_id]
        )

    prob += bank + pulp.lpSum(
        [p["cost"] * out_vars[p["id"]] for p in squad]
    ) >= pulp.lpSum([c["cost"] * in_vars[c["id"]] for c in candidates])

    remaining_by_team = {}
    for p in squad:
        remaining_by_team.setdefault(p["team"], []).append(p)
    all_teams = {p["team"] for p in squad} | {c["team"] for c in candidates}
    for team_id in all_teams:
        kept = [p for p in squad if p["team"] == team_id]
        prob += (
            pulp.lpSum([1 - out_vars[p["id"]] for p in kept])
            + pulp.lpSum([in_vars[c["id"]] for c in candidates if c["team"] == team_id])
            <= 3
        )

    status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[status] != "Optimal":
        return [], bank, 0.0

    players_out = [p for p in squad if pulp.value(out_vars[p["id"]]) == 1]
    players_in = [c for c in candidates if pulp.value(in_vars[c["id"]]) == 1]

    total_gain = sum(c["ep_next"] for c in players_in) - sum(p["ep_next"] for p in players_out)
    if total_gain <= 0.5:
        return [], bank, 0.0

    remaining_bank = (
        bank + sum(p["cost"] for p in players_out) - sum(c["cost"] for c in players_in)
    )

    swaps = []
    ins_by_pos = {}
    for c in players_in:
        ins_by_pos.setdefault(c["pos_id"], []).append(c)
    for p in players_out:
        pool = ins_by_pos.get(p["pos_id"], [])
        partner = pool.pop() if pool else None
        gain = (partner["ep_next"] - p["ep_next"]) if partner else 0.0
        swaps.append((p, partner, gain))

    return swaps, remaining_bank, total_gain


def solve_chip_squad(horizon_gws=1):
    """Calculates best 15-man squad for Free Hit (1 GW) or Wildcard (5-10 GWs)."""
    data = get_fpl_bootstrap()
    types = {t["id"]: t["singular_name_short"] for t in data["element_types"]}

    players = []
    for p in data["elements"]:
        single_xp = float(p["ep_next"]) if p["ep_next"] is not None else 0.0
        form_xp = float(p["form"]) if p["form"] is not None else 0.0

        score = (
            single_xp
            if horizon_gws == 1
            else (single_xp * 0.5 + form_xp * 0.5) * horizon_gws
        )

        players.append(
            {
                "id": p["id"],
                "name": p["web_name"],
                "pos": types[p["element_type"]],
                "cost": p["now_cost"] / 10.0,
                "team": p["team"],
                "xp": round(score, 1),
            }
        )

    prob = pulp.LpProblem("Chip_Opt", pulp.LpMaximize)
    vars = {
        p["id"]: pulp.LpVariable(f"p_{p['id']}", cat="Binary") for p in players
    }

    prob += pulp.lpSum([p["xp"] * vars[p["id"]] for p in players])
    prob += pulp.lpSum([p["cost"] * vars[p["id"]] for p in players]) <= 100.0
    prob += pulp.lpSum([vars[p["id"]] for p in players]) == 15

    prob += (
        pulp.lpSum([vars[p["id"]] for p in players if p["pos"] == "GKP"]) == 2
    )
    prob += (
        pulp.lpSum([vars[p["id"]] for p in players if p["pos"] == "DEF"]) == 5
    )
    prob += (
        pulp.lpSum([vars[p["id"]] for p in players if p["pos"] == "MID"]) == 5
    )
    prob += (
        pulp.lpSum([vars[p["id"]] for p in players if p["pos"] == "FWD"]) == 3
    )

    for team_id in range(1, 21):
        prob += (
            pulp.lpSum([vars[p["id"]] for p in players if p["team"] == team_id])
            <= 3
        )

    status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[status] != "Optimal":
        return []
    return [p for p in players if pulp.value(vars[p["id"]]) == 1]


# --- TELEGRAM COMMAND HANDLERS ---


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *FPL Assistant Bot Online*\n\n"
        "Available commands:\n"
        "• `/setteam <ID>` — Link your FPL Team ID\n"
        "• `/squad` — Calculate optimal Starting XI, Captain & Bench\n"
        "• `/transfers [1 or 2]` — Optimal transfer recommendations\n"
        "• `/hits <num_hits>` — Evaluate if taking transfer point hits is worth it\n"
        "• `/benchboost` — Calculate optimal lineup with Bench Boost active\n"
        "• `/triplecaptain` — Optimal lineup with Triple Captain active\n"
        "• `/freehit` — Maximum 1-GW Free Hit squad\n"
        "• `/wildcard` — Multi-GW Wildcard squad (5-10 GW horizon)\n"
        "• `/stats` — View your team performance stats & overall rank\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def setteam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "❌ Usage: `/setteam <Your_FPL_ID>`", parse_mode="Markdown"
        )
        return

    team_id = context.args[0]
    if not team_id.isdigit():
        await update.message.reply_text(
            "❌ That doesn't look like a valid FPL Team ID (should be numeric)."
        )
        return

    set_team_id(update.effective_chat.id, team_id)
    await update.message.reply_text(
        f"✅ FPL Team ID linked: *{team_id}*", parse_mode="Markdown"
    )


async def squad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team_id = get_team_id(update.effective_chat.id)
    if not team_id:
        await update.message.reply_text(
            "⚠️ Link your team first using `/setteam <ID>`."
        )
        return

    await update.message.reply_text(
        "🔄 Optimizing your starting XI and bench order..."
    )
    squad, _, next_gw, _ = fetch_user_squad(team_id)

    if not squad:
        await update.message.reply_text(
            "❌ Unable to fetch squad. Check Settings → Privacy on the FPL site."
        )
        return

    starters, bench = solve_starting_xi(squad)
    if not starters:
        await update.message.reply_text(
            "❌ Couldn't compute a valid lineup from your squad data. Try again shortly."
        )
        return

    total_xp = sum(p["ep_next"] for p in starters) + sum(
        p["ep_next"] for p in starters if p.get("is_captain")
    )

    msg = f"📋 *GW{next_gw} Optimal Lineup* (Team: {team_id})\n"
    msg += f"📊 *Projected Starting Score:* {total_xp:.1f} xP\n\n🟢 *STARTING XI*\n"

    for p in starters:
        role = ""
        if p.get("is_captain"):
            role = " *(C)*"
        elif p.get("is_vice"):
            role = " *(VC)*"
        msg += f"• [{p['pos']}] *{p['name']}*{role} — {p['ep_next']} xP\n"

    msg += "\n🪑 *BENCH ROTATION ORDER*\n"
    for idx, p in enumerate(bench, 1):
        msg += f"{idx}. [{p['pos']}] {p['name']} — {p['ep_next']} xP\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def transfers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team_id = get_team_id(update.effective_chat.id)
    if not team_id:
        await update.message.reply_text(
            "⚠️ Link your team first using `/setteam <ID>`."
        )
        return

    num_transfers = 1
    if context.args:
        try:
            num_transfers = min(max(int(context.args[0]), 1), 2)
        except ValueError:
            num_transfers = 1

    await update.message.reply_text(
        f"⏳ Analyzing top {num_transfers} transfer recommendation(s)..."
    )

    squad, bank, next_gw, _ = fetch_user_squad(team_id)
    if not squad:
        await update.message.reply_text("❌ Unable to fetch squad.")
        return

    swaps, remaining_bank, total_gain = solve_transfers(
        squad, bank, num_transfers
    )

    if not swaps:
        await update.message.reply_text(
            f"✅ Your team is already optimal for GW{next_gw}. No immediate transfers recommended."
        )
        return

    msg = f"🔄 *RECOMMENDED TRANSFER PLAN (GW{next_gw})*\n\n"
    for out_p, in_p, gain in swaps:
        msg += f"🔴 *OUT:* [{out_p['pos']}] {out_p['name']} (£{out_p['cost']}m)\n"
        if in_p:
            msg += f"🟢 *IN:* [{in_p['pos']}] {in_p['name']} (£{in_p['cost']}m)\n"
        msg += f"📈 *Projected Gain:* +{gain:.2f} xP\n\n"

    msg += f"💰 *Remaining Bank:* £{remaining_bank:.1f}m\n"
    msg += f"📊 *Total Expected Gain:* +{total_gain:.2f} xP"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def hits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Calculates if taking a transfer point hit (costing 4 points per extra transfer) is mathematically worth it."""
    team_id = get_team_id(update.effective_chat.id)
    if not team_id:
        await update.message.reply_text(
            "⚠️ Link your team first using `/setteam <ID>`."
        )
        return

    num_hits = 1
    if context.args:
        try:
            num_hits = max(int(context.args[0]), 1)
        except ValueError:
            num_hits = 1

    # Each extra transfer costs 4 points
    cost_in_points = num_hits * 4

    await update.message.reply_text(
        f"⚖️ Evaluating if taking {num_hits} extra transfer(s) (costing {cost_in_points} pts) is worth it..."
    )

    squad, bank, next_gw, _ = fetch_user_squad(team_id)
    if not squad:
        await update.message.reply_text("❌ Unable to fetch squad.")
        return

    # Evaluate optimal transfers for (1 free transfer + num_hits extra transfers)
    swaps, remaining_bank, total_gain = solve_transfers(
        squad, bank, num_transfers=1 + num_hits
    )

    if not swaps:
        await update.message.reply_text(
            f"❌ Could not find valid transfers for {1 + num_hits} moves."
        )
        return

    net_gain = total_gain - cost_in_points
    is_worth_it = net_gain > 0

    msg = f"⚖️ *TRANSFER HIT ANALYSIS (GW{next_gw})\n\n"
    msg += f"• Extra Transfers / Hit Cost: *{num_hits} (-{cost_in_points} pts)*\n"
    msg += f"• Projected xP Gain from Transfers: *+{total_gain:.2f} xP*\n"
    msg += f"• Net Expected Gain: *{net_gain:+.2f} xP*\n\n"

    if is_worth_it:
        msg += "✅ *Verdict:* **WORTH IT!** The expected points gain outweighs the hit cost.\n\n"
    else:
        msg += "❌ *Verdict:* **NOT RECOMMENDED.** The expected gain does not cover the 4-point penalty cost.\n\n"

    msg += "🔄 *Proposed Moves:*\n"
    for out_p, in_p, gain in swaps:
        msg += f"• OUT: {out_p['name']} | IN: {in_p.get('name', 'None')} (+{gain:.2f} xP)\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def benchboost_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Calculates optimal lineup with Bench Boost chip active."""
    team_id = get_team_id(update.effective_chat.id)
    if not team_id:
        await update.message.reply_text(
            "⚠️ Link your team first using `/setteam <ID>`."
        )
        return

    await update.message.reply_text("🚀 Calculating optimal squad performance with **Bench Boost** active...")
    squad, _, next_gw, _ = fetch_user_squad(team_id)
    if not squad:
        await update.message.reply_text("❌ Unable to fetch squad.")
        return

    starters, bench = solve_starting_xi(squad, chip='benchboost')
    if not starters:
        await update.message.reply_text("❌ Couldn't compute a valid lineup.")
        return

    total_xp = sum(p["ep_next"] for p in starters) + sum(p["ep_next"] for p in bench)

    msg = f"🚀 *GW{next_gw} BENCH BOOST LINEUP*\n"
    msg += f"📊 *Total Projected Score (All 15):* {total_xp:.1f} xP\n\n🟢 *STARTING XI*\n"
    for p in starters:
        msg += f"• [{p['pos']}] *{p['name']}* — {p['ep_next']} xP\n"

    msg += "\n🪑 *BENCH (Now Scoring Points!)*\n"
    for p in bench:
        msg += f"• [{p['pos']}] *{p['name']}* — {p['ep_next']} xP\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def triplecaptain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Calculates optimal lineup with Triple Captain chip active."""
    team_id = get_team_id(update.effective_chat.id)
    if not team_id:
        await update.message.reply_text(
            "⚠️ Link your team first using `/setteam <ID>`."
        )
        return

    await update.message.reply_text("⭐ Calculating optimal squad performance with **Triple Captain** active...")
    squad, _, next_gw, _ = fetch_user_squad(team_id)
    if not squad:
        await update.message.reply_text("❌ Unable to fetch squad.")
        return

    starters, bench = solve_starting_xi(squad, chip='triplecaptain')
    if not starters:
        await update.message.reply_text("❌ Couldn't compute a valid lineup.")
        return

    total_xp = sum(p["ep_next"] for p in starters) + 2 * sum(
        p["ep_next"] for p in starters if p.get("is_captain")
    )

    msg = f"⭐ *GW{next_gw} TRIPLE CAPTAIN LINEUP*\n"
    msg += f"📊 *Projected Score (Captain x3):* {total_xp:.1f} xP\n\n🟢 *STARTING XI*\n"
    for p in starters:
        role = ""
        if p.get("is_captain"):
            role = " *(TRIPLE CAPTAIN)*"
        elif p.get("is_vice"):
            role = " *(VC)*"
        msg += f"• [{p['pos']}] *{p['name']}*{role} — {p['ep_next']} xP\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays user performance statistics, overall rank, and team details."""
    team_id = get_team_id(update.effective_chat.id)
    if not team_id:
        await update.message.reply_text(
            "⚠️ Link your team first using `/setteam <ID>`."
        )
        return

    await update.message.reply_text("📊 Fetching your team statistics...")
    _, _, next_gw, entry_info = fetch_user_squad(team_id)

    if not entry_info:
        await update.message.reply_text("❌ Could not retrieve team stats. Check Team ID.")
        return

    team_name = entry_info.get("name", "Unknown Team")
    player_name = f"{entry_info.get('player_first_name', '')} {entry_info.get('player_last_name', '')}".strip()
    overall_points = entry_info.get("summary_overall_points", "N/A")
    overall_rank = entry_info.get("summary_overall_rank", "N/A")
    team_value = entry_info.get("last_deadline_value", 0) / 10.0
    bank = entry_info.get("last_deadline_bank", 0) / 10.0

    msg = f"📊 *TEAM STATISTICS & PROFILE*\n\n"
    msg += f"• *Team Name:* {team_name}\n"
    msg += f"• *Manager:* {player_name}\n"
    msg += f"• *Overall Points:* {overall_points}\n"
    msg += f"• *Overall Rank:* {f'{overall_rank:,}' if isinstance(overall_rank, int) else overall_rank}\n"
    msg += f"• *Squad Value:* £{team_value:.1f}m\n"
    msg += f"• *Bank Balance:* £{bank:.1f}m\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def freehit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🃏 Calculating optimal Free Hit squad for maximum single GW points..."
    )
    squad = solve_chip_squad(horizon_gws=1)
    if not squad:
        await update.message.reply_text("❌ Couldn't compute a Free Hit squad right now.")
        return

    msg = "🔥 *OPTIMAL FREE HIT SQUAD*\n\n"
    total_cost = sum(p["cost"] for p in squad)

    for pos in ["GKP", "DEF", "MID", "FWD"]:
        msg += f"*{pos}s:*\n"
        for p in squad:
            if p["pos"] == pos:
                msg += f"• {p['name']} (£{p['cost']}m)\n"
        msg += "\n"

    msg += f"💰 *Total Cost:* £{total_cost:.1f}m / £100.0m"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def wildcard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔮 Calculating optimal Wildcard squad over a 5-10 GW horizon..."
    )
    squad = solve_chip_squad(horizon_gws=5)
    if not squad:
        await update.message.reply_text("❌ Couldn't compute a Wildcard squad right now.")
        return

    msg = "🃏 *OPTIMAL WILDCARD SQUAD (Multi-GW Horizon)*\n\n"
    total_cost = sum(p["cost"] for p in squad)

    for pos in ["GKP", "DEF", "MID", "FWD"]:
        msg += f"*{pos}s:*\n"
        for p in squad:
            if p["pos"] == pos:
                msg += f"• {p['name']} (£{p['cost']}m)\n"
        msg += "\n"

    msg += f"💰 *Total Cost:* £{total_cost:.1f}m / £100.0m"
    await update.message.reply_text(msg, parse_mode="Markdown")


# --- FLASK & TELEGRAM WEBHOOK INTEGRATION ---

flask_app = Flask(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set!")

telegram_app = ApplicationBuilder().token(TOKEN).build()

telegram_app.add_handler(CommandHandler("start", start_cmd))
telegram_app.add_handler(CommandHandler("setteam", setteam_cmd))
telegram_app.add_handler(CommandHandler("squad", squad_cmd))
telegram_app.add_handler(CommandHandler("transfers", transfers_cmd))
telegram_app.add_handler(CommandHandler("hits", hits_cmd))
telegram_app.add_handler(CommandHandler("benchboost", benchboost_cmd))
telegram_app.add_handler(CommandHandler("triplecaptain", triplecaptain_cmd))
telegram_app.add_handler(CommandHandler("freehit", freehit_cmd))
telegram_app.add_handler(CommandHandler("wildcard", wildcard_cmd))
telegram_app.add_handler(CommandHandler("stats", stats_cmd))

# One persistent event loop for the whole process, run on a background
# thread. This fixes the original design, which called asyncio.run()
# (a NEW event loop) on every single webhook hit and re-initialized
# telegram_app.initialize() every time — wasteful, and under concurrent
# requests could race or lose updates.
_bot_loop = asyncio.new_event_loop()


def _start_loop():
    asyncio.set_event_loop(_bot_loop)
    _bot_loop.run_forever()


_loop_thread = threading.Thread(target=_start_loop, daemon=True)
_loop_thread.start()

# Initialize the PTB application once, on the persistent loop, before
# any requests are served.
asyncio.run_coroutine_threadsafe(telegram_app.initialize(), _bot_loop).result()


@flask_app.route("/")
def health_check():
    return "FPL Telegram Bot is running live via Webhooks!", 200


@flask_app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """Endpoint that receives updates directly from Telegram."""
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)

    # Hand the update to the persistent event loop instead of spinning
    # up a new one per request.
    asyncio.run_coroutine_threadsafe(
        telegram_app.process_update(update), _bot_loop
    )
    return "OK", 200


def _set_webhook_once():
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if not render_url:
        return
    webhook_url = f"{render_url}/{TOKEN}"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/setWebhook",
            data={"url": webhook_url},
            timeout=10,
        )
        resp.raise_for_status()
        print(f"Webhook set to: {webhook_url}")
    except requests.RequestException as e:
        print(f"Failed to set webhook: {e}")


_set_webhook_once()

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=10000)
