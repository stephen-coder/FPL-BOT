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


# --- ADVANCED FPL DATA FETCHERS & FIXTURE ENGINE ---

FPL_BASE_URL = "https://fantasy.premierleague.com/api/"

FPL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://fantasy.premierleague.com/api/",
}


def get_fpl_bootstrap():
    """Fetches core database metrics from the official FPL API."""
    url = f"{FPL_BASE_URL}bootstrap-static/"
    res = requests.get(url, headers=FPL_HEADERS, timeout=15)
    res.raise_for_status()
    return res.json()


def get_fixtures():
    """Fetches full season fixture list to analyze upcoming difficulties."""
    url = f"{FPL_BASE_URL}fixtures/"
    try:
        res = requests.get(url, headers=FPL_HEADERS, timeout=15)
        if res.status_code == 200:
            return res.json()
    except requests.RequestException:
        pass
    return []


def get_next_gw(data):
    """Finds the upcoming active Gameweek ID."""
    for gw in data["events"]:
        if gw["is_next"]:
            return gw["id"]
    return 1


def calculate_horizon_xp(player, team_id_to_fixtures, current_gw, horizon=3):
    """Calculates rolling multi-week expected points adjusted by fixture difficulty."""
    base_ep = float(player.get("ep_next") or 0.0)
    if base_ep <= 0:
        return 0.0

    team_fixtures = team_id_to_fixtures.get(player["team"], [])
    upcoming = [f for f in team_fixtures if f.get("event") and current_gw <= f["event"] < current_gw + horizon]

    if not upcoming:
        return base_ep * horizon

    total_weighted_xp = 0.0
    decay_factors = [1.0, 0.85, 0.7]

    for idx, fix in enumerate(upcoming[:horizon]):
        decay = decay_factors[idx] if idx < len(decay_factors) else 0.5
        is_home = fix.get("team_h") == player["team"]
        diff = fix.get("team_h_difficulty" if is_home else "team_a_difficulty", 3)
        
        diff_multiplier = {1: 1.2, 2: 1.1, 3: 1.0, 4: 0.85, 5: 0.7}.get(diff, 1.0)
        total_weighted_xp += base_ep * diff_multiplier * decay

    return round(total_weighted_xp, 2)


def fetch_user_squad(team_id):
    """Pulls user squad, bank, next GW, and computes multi-week horizon xP."""
    data = get_fpl_bootstrap()
    next_gw = get_next_gw(data)
    prev_gw = max(1, next_gw - 1)

    url = f"{FPL_BASE_URL}entry/{team_id}/event/{prev_gw}/picks/"
    try:
        res = requests.get(url, headers=FPL_HEADERS, timeout=15)
    except requests.RequestException:
        return None, 0.0, next_gw, {}

    if res.status_code != 200:
        return None, 0.0, next_gw, {}

    picks_data = res.json()
    picks = [p["element"] for p in picks_data["picks"]]
    bank = picks_data.get("entry_history", {}).get("bank", 0) / 10.0

    entry_url = f"{FPL_BASE_URL}entry/{team_id}/"
    entry_res = requests.get(entry_url, headers=FPL_HEADERS, timeout=15)
    entry_info = entry_res.json() if entry_res.status_code == 200 else {}

    types = {t["id"]: t["singular_name_short"] for t in data["element_types"]}
    players_by_id = {p["id"]: p for p in data["elements"]}

    fixtures = get_fixtures()
    team_fixtures = {}
    for f in fixtures:
        if f.get("event"):
            team_fixtures.setdefault(f["team_h"], []).append(f)
            team_fixtures.setdefault(f["team_a"], []).append(f)

    squad = []
    for pid in picks:
        if pid in players_by_id:
            p = players_by_id[pid]
            horizon_xp = calculate_horizon_xp(p, team_fixtures, next_gw, horizon=3)
            squad.append(
                {
                    "id": p["id"],
                    "name": p["web_name"],
                    "pos": types[p["element_type"]],
                    "pos_id": p["element_type"],
                    "ep_next": float(p["ep_next"]) if p["ep_next"] is not None else 0.0,
                    "horizon_xp": horizon_xp,
                    "cost": p["now_cost"] / 10.0,
                    "team": p["team"],
                    "selected_by": float(p.get("selected_by_percent", 0.0)),
                }
            )
    return squad, bank, next_gw, entry_info


# --- PULP OPTIMIZATION ENGINES (WITH HORIZON & DIFFERENTIALS) ---

def solve_starting_xi(squad, chip=None, use_horizon=False):
    """
    Optimizes Starting 11, Captain (C), Vice Captain (VC), Bench, 
    and detects Safe vs Differential Captains.
    """
    if not squad:
        return [], [], None, None

    prob = pulp.LpProblem("Lineup_Opt", pulp.LpMaximize)
    scoring_key = "horizon_xp" if use_horizon else "ep_next"

    start_vars = {p["id"]: pulp.LpVariable(f"start_{p['id']}", cat="Binary") for p in squad}
    cap_vars = {p["id"]: pulp.LpVariable(f"cap_{p['id']}", cat="Binary") for p in squad}

    cap_multiplier = 3 if chip == 'triplecaptain' else 2

    if chip == 'benchboost':
        prob += pulp.lpSum([p[scoring_key] for p in squad]) + pulp.lpSum(
            [(cap_multiplier - 1) * p[scoring_key] * cap_vars[p["id"]] for p in squad]
        )
        prob += pulp.lpSum([start_vars[p["id"]] for p in squad]) == 15
    else:
        prob += pulp.lpSum([p[scoring_key] * start_vars[p["id"]] for p in squad]) + pulp.lpSum(
            [(cap_multiplier - 1) * p[scoring_key] * cap_vars[p["id"]] for p in squad]
        )
        prob += pulp.lpSum([start_vars[p["id"]] for p in squad]) == 11, "Exactly_11_Starters"

    prob += pulp.lpSum([cap_vars[p["id"]] for p in squad]) == 1, "Exactly_1_Captain"

    for p in squad:
        prob += cap_vars[p["id"]] <= start_vars[p["id"]]

    gkps = [p for p in squad if p["pos"] == "GKP"]
    defs = [p for p in squad if p["pos"] == "DEF"]
    mids = [p for p in squad if p["pos"] == "MID"]
    fwds = [p for p in squad if p["pos"] == "FWD"]

    prob += pulp.lpSum([start_vars[p["id"]] for p in gkps]) == (2 if chip == 'benchboost' else 1)
    prob += pulp.lpSum([start_vars[p["id"]] for p in defs]) >= 3
    prob += pulp.lpSum([start_vars[p["id"]] for p in defs]) <= 5
    prob += pulp.lpSum([start_vars[p["id"]] for p in mids]) >= 2
    prob += pulp.lpSum([start_vars[p["id"]] for p in mids]) <= 5
    prob += pulp.lpSum([start_vars[p["id"]] for p in fwds]) >= 1
    prob += pulp.lpSum([start_vars[p["id"]] for p in fwds]) <= 3

    status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[status] != "Optimal":
        return [], [], None, None

    starters, bench = [], []
    for p in squad:
        p["is_captain"] = pulp.value(cap_vars[p["id"]]) == 1
        if pulp.value(start_vars[p["id"]]) == 1:
            starters.append(p)
        else:
            bench.append(p)

    sorted_starters = sorted(starters, key=lambda x: x[scoring_key], reverse=True)
    safe_captain = sorted_starters[0] if sorted_starters else None

    differentials = [p for p in starters if p["selected_by"] < 10.0]
    differential_captain = max(differentials, key=lambda x: x[scoring_key]) if differentials else safe_captain

    bench_gkp = [p for p in bench if p["pos"] == "GKP"]
    bench_outfield = sorted([p for p in bench if p["pos"] != "GKP"], key=lambda x: x[scoring_key], reverse=True)

    non_cap_starters = sorted([p for p in starters if not p["is_captain"]], key=lambda x: x[scoring_key], reverse=True)
    vc_id = non_cap_starters[0]["id"] if non_cap_starters else None
    for p in starters:
        p["is_vice"] = p["id"] == vc_id

    return starters, bench_gkp + bench_outfield, safe_captain, differential_captain


def solve_transfers(squad, bank, num_transfers=1):
    """Jointly optimizes transfer moves using 3-week horizon xP projections."""
    if not squad:
        return [], bank, 0.0

    data = get_fpl_bootstrap()
    types = {t["id"]: t["singular_name_short"] for t in data["element_types"]}
    fixtures = get_fixtures()
    team_fixtures = {}
    for f in fixtures:
        if f.get("event"):
            team_fixtures.setdefault(f["team_h"], []).append(f)
            team_fixtures.setdefault(f["team_a"], []).append(f)

    current_gw = get_next_gw(data)
    current_ids = {p["id"] for p in squad}
    candidates = []

    for p in data["elements"]:
        if p["id"] in current_ids:
            continue
        if p["status"] == "a" and float(p["ep_next"] or 0) > 0:
            horizon_xp = calculate_horizon_xp(p, team_fixtures, current_gw, horizon=3)
            candidates.append(
                {
                    "id": p["id"],
                    "name": p["web_name"],
                    "pos": types[p["element_type"]],
                    "pos_id": p["element_type"],
                    "cost": p["now_cost"] / 10.0,
                    "team": p["team"],
                    "ep_next": float(p["ep_next"] or 0),
                    "horizon_xp": horizon_xp,
                }
            )

    prob = pulp.LpProblem("Transfer_Opt", pulp.LpMaximize)

    out_vars = {p["id"]: pulp.LpVariable(f"out_{p['id']}", cat="Binary") for p in squad}
    in_vars = {c["id"]: pulp.LpVariable(f"in_{c['id']}", cat="Binary") for c in candidates}

    prob += pulp.lpSum([c["horizon_xp"] * in_vars[c["id"]] for c in candidates]) - pulp.lpSum(
        [p["horizon_xp"] * out_vars[p["id"]] for p in squad]
    )

    prob += pulp.lpSum(out_vars.values()) == num_transfers
    prob += pulp.lpSum(in_vars.values()) == num_transfers

    for pos_id in {p["pos_id"] for p in squad}:
        prob += pulp.lpSum([out_vars[p["id"]] for p in squad if p["pos_id"] == pos_id]) == pulp.lpSum(
            [in_vars[c["id"]] for c in candidates if c["pos_id"] == pos_id]
        )

    prob += bank + pulp.lpSum([p["cost"] * out_vars[p["id"]] for p in squad]) >= pulp.lpSum(
        [c["cost"] * in_vars[c["id"]] for c in candidates]
    )

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

    total_gain = sum(c["horizon_xp"] for c in players_in) - sum(p["horizon_xp"] for p in players_out)
    if total_gain <= 0.5:
        return [], bank, 0.0

    remaining_bank = bank + sum(p["cost"] for p in players_out) - sum(c["cost"] for c in players_in)

    swaps = []
    ins_by_pos = {}
    for c in players_in:
        ins_by_pos.setdefault(c["pos_id"], []).append(c)
    for p in players_out:
        pool = ins_by_pos.get(p["pos_id"], [])
        partner = pool.pop() if pool else None
        gain = (partner["horizon_xp"] - p["horizon_xp"]) if partner else 0.0
        swaps.append((p, partner, gain))

    return swaps, remaining_bank, total_gain


def analyze_chip_timing():
    """Scans upcoming gameweeks to detect Double (DGW) and Blank (BGW) Gameweeks."""
    data = get_fpl_bootstrap()
    fixtures = get_fixtures()
    next_gw = get_next_gw(data)

    gw_analysis = []
    for gw_id in range(next_gw, min(next_gw + 6, 39)):
        gw_fixtures = [f for f in fixtures if f.get("event") == gw_id]
        team_match_counts = {}
        for f in gw_fixtures:
            team_match_counts[f["team_h"]] = team_match_counts.get(f["team_h"], 0) + 1
            team_match_counts[f["team_a"]] = team_match_counts.get(f["team_a"], 0) + 1

        doubles = [team_id for team_id, count in team_match_counts.items() if count > 1]
        blanks = [team_id for team_id in range(1, 21) if team_match_counts.get(team_id, 0) == 0]

        gw_analysis.append({
            "gw": gw_id,
            "doubles": len(doubles),
            "blanks": len(blanks),
            "total_fixtures": len(gw_fixtures)
        })
    return gw_analysis


# --- TELEGRAM COMMAND HANDLERS ---

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *Elite FPL AI Assistant Online*\n\n"
        "Advanced Commands:\n"
        "• `/setteam <ID>` — Link your FPL Team ID\n"
        "• `/squad` — 3-Week Horizon Lineup, Captains & Bench\n"
        "• `/transfers [1 or 2]` — Multi-week fixture transfer planner\n"
        "• `/hits <num>` — Point hit ROI evaluator\n"
        "• `/bestchip` — Scans upcoming DGWs/BGWs for optimal chip timing\n"
        "• `/live` — Real-time live score tracker for your squad\n"
        "• `/prices` — Track upcoming price risers and fallers\n"
        "• `/stats` — Manager rank & team valuation\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def setteam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: `/setteam <Your_FPL_ID>`", parse_mode="Markdown")
        return
    team_id = context.args[0]
    if not team_id.isdigit():
        await update.message.reply_text("❌ Invalid FPL Team ID (must be numeric).")
        return
    set_team_id(update.effective_chat.id, team_id)
    await update.message.reply_text(f"✅ FPL Team ID linked: *{team_id}*", parse_mode="Markdown")


async def squad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team_id = get_team_id(update.effective_chat.id)
    if not team_id:
        await update.message.reply_text("⚠️ Link your team first using `/setteam <ID>`.")
        return

    await update.message.reply_text("🔄 Analyzing 3-week fixture horizon, starting XI, and differential captains...")
    squad, _, next_gw, _ = fetch_user_squad(team_id)

    if not squad:
        await update.message.reply_text("❌ Unable to fetch squad. Check FPL privacy settings.")
        return

    starters, bench, safe_c, diff_c = solve_starting_xi(squad, use_horizon=True)
    if not starters:
        await update.message.reply_text("❌ Couldn't compute optimal lineup.")
        return

    total_horizon_xp = sum(p["horizon_xp"] for p in starters) + (safe_c["horizon_xp"] if safe_c else 0)

    msg = f"📋 *GW{next_gw} 3-Week Horizon Lineup*\n"
    msg += f"📊 *Projected 3-GW Score:* {total_horizon_xp:.1f} xP\n\n"

    if safe_c:
        msg += f"⭐ **Safe Captain:** {safe_c['name']} ({safe_c['horizon_xp']} xP)\n"
    if diff_c and diff_c['id'] != safe_c['id']:
        msg += f"🎯 **Differential Captain (<10% ownership):** {diff_c['name']} ({diff_c['selected_by']}% owned, {diff_c['horizon_xp']} xP)\n\n"

    msg += "🟢 *STARTING XI (Horizon xP)*\n"
    for p in starters:
        role = " *(C)*" if p.get("is_captain") else (" *(VC)*" if p.get("is_vice") else "")
        msg += f"• [{p['pos']}] *{p['name']}*{role} — {p['horizon_xp']} xP\n"

    msg += "\n🪑 *BENCH ROTATION*\n"
    for idx, p in enumerate(bench, 1):
        msg += f"{idx}. [{p['pos']}] {p['name']} — {p['horizon_xp']} xP\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def transfers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team_id = get_team_id(update.effective_chat.id)
    if not team_id:
        await update.message.reply_text("⚠️ Link your team first using `/setteam <ID>`.")
        return

    num_transfers = 1
    if context.args:
        try:
            num_transfers = min(max(int(context.args[0]), 1), 2)
        except ValueError:
            num_transfers = 1

    await update.message.reply_text(f"⏳ Running multi-week fixture transfer simulation for {num_transfers} move(s)...")

    squad, bank, next_gw, _ = fetch_user_squad(team_id)
    if not squad:
        await update.message.reply_text("❌ Unable to fetch squad.")
        return

    swaps, remaining_bank, total_gain = solve_transfers(squad, bank, num_transfers)

    if not swaps:
        await update.message.reply_text(f"✅ Your squad has optimal fixture coverage for GW{next_gw}. No urgent transfers needed.")
        return

    msg = f"🔄 *MULTI-WEEK TRANSFER PLAN (GW{next_gw} + 2)*\n\n"
    for out_p, in_p, gain in swaps:
        msg += f"🔴 *OUT:* [{out_p['pos']}] {out_p['name']} (£{out_p['cost']}m)\n"
        if in_p:
            msg += f"🟢 *IN:* [{in_p['pos']}] {in_p['name']} (£{in_p['cost']}m)\n"
        msg += f"📈 *3-GW Horizon Gain:* +{gain:.2f} xP\n\n"

    msg += f"💰 *Remaining Bank:* £{remaining_bank:.1f}m\n"
    msg += f"📊 *Total Horizon Gain:* +{total_gain:.2f} xP\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def hits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team_id = get_team_id(update.effective_chat.id)
    if not team_id:
        await update.message.reply_text("⚠️ Link your team first using `/setteam <ID>`.")
        return

    num_hits = 1
    if context.args:
        try:
            num_hits = max(int(context.args[0]), 1)
        except ValueError:
            num_hits = 1

    total_transfers = 1 + num_hits
    cost_in_points = num_hits * 4

    squad, bank, next_gw, _ = fetch_user_squad(team_id)
    if not squad:
        await update.message.reply_text("❌ Unable to fetch squad.")
        return

    swaps, _, total_gain = solve_transfers(squad, bank, num_transfers=total_transfers)
    if not swaps:
        await update.message.reply_text("❌ Could not compute valid transfers for this hit.")
        return

    net_gain = total_gain - cost_in_points
    worth = net_gain > 0

    msg = f"⚖️ *HIT ROI ANALYSIS (-{cost_in_points} pts)*\n\n"
    msg += f"• 3-Week Horizon Gain: *+{total_gain:.2f} xP*\n"
    msg += f"• Net Gain after Hit: *{net_gain:+.2f} xP*\n"
    msg += f"• Verdict: {'✅ **WORTH IT**' if worth else '❌ **NOT RECOMMENDED**'}\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def bestchip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Scans upcoming gameweeks for Double and Blank gameweeks to time chip usage."""
    await update.message.reply_text("🔍 Scanning upcoming fixture schedule for Double & Blank Gameweeks...")
    analysis = analyze_chip_timing()

    msg = "📅 *CHIP STRATEGY TIMING ENGINE*\n\n"
    for gw in analysis:
        status_icon = "🟢 Normal"
        if gw["doubles"] > 0:
            status_icon = f"🔥 **Double GW ({gw['doubles']} teams)** -> *Ideal for Bench Boost / Triple Captain*"
        elif gw["blanks"] > 3:
            status_icon = f"⚠️ **Blank GW ({gw['blanks']} teams blanking)** -> *Ideal for Free Hit*"
        
        msg += f"• **GW{gw['gw']}:** {status_icon}\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def live_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tracks real-time points for your squad in the current active gameweek."""
    team_id = get_team_id(update.effective_chat.id)
    if not team_id:
        await update.message.reply_text("⚠️ Link your team first using `/setteam <ID>`.")
        return

    data = get_fpl_bootstrap()
    current_gw = None
    for gw in data["events"]:
        if gw["is_current"]:
            current_gw = gw["id"]
            break

    if not current_gw:
        await update.message.reply_text("⏳ No gameweek is currently live right now. Check back when matches kick off!")
        return

    url = f"{FPL_BASE_URL}entry/{team_id}/event/{current_gw}/picks/"
    try:
        res = requests.get(url, headers=FPL_HEADERS, timeout=15)
        if res.status_code != 200:
            await update.message.reply_text("❌ Could not fetch live squad data.")
            return
        picks_data = res.json()
    except requests.RequestException:
        await update.message.reply_text("❌ Network error fetching live scores.")
        return

    players_by_id = {p["id"]: p for p in data["elements"]}
    picks = picks_data.get("picks", [])
    entry_history = picks_data.get("entry_history", {})
    
    total_points = entry_history.get("points", 0)
    event_transfers_cost = entry_history.get("event_transfers_cost", 0)

    msg = f"🔴 *GW{current_gw} LIVE TRACKER*\n"
    msg += f"📊 *Live Points (Net):* {total_points - event_transfers_cost} pts (Hits: -{event_transfers_cost})\n\n"
    msg += "⚽ *Starting XI Live Scores*\n"

    for p in picks[:11]:
        pid = p["element"]
        mult = p["multiplier"]
        player_info = players_by_id.get(pid, {})
        name = player_info.get("web_name", "Unknown")
        match_pts = player_info.get("event_points", 0)
        
        role = ""
        if mult == 2:
            role = " *(C)*"
        elif mult == 3:
            role = " *(TC)*"
        elif mult == 0:
            role = " *(Bench)*"

        msg += f"• {name}{role}: **{match_pts * max(1, mult)} pts** ({match_pts} x {max(1, mult)})\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def prices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tracks players closest to rising or falling in price based on net transfers."""
    data = get_fpl_bootstrap()
    elements = data["elements"]

    # Sort by cost change likelihood (transfers_in_event - transfers_out_event roughly approximates net change momentum)
    sorted_risers = sorted(elements, key=lambda x: x.get("transfers_in_event", 0), reverse=True)[:5]
    sorted_fallers = sorted(elements, key=lambda x: x.get("transfers_out_event", 0), reverse=True)[:5]

    msg = "💰 *FPL PRICE CHANGE WATCHLIST*\n\n"
    msg += "🔥 **Top Potential Risers (Inbound Momentum)**\n"
    for p in sorted_risers:
        cost = p["now_cost"] / 10.0
        net_in = p.get("transfers_in_event", 0)
        msg += f"• {p['web_name']} (£{cost}m) — +{net_in:,} transfers\n"

    msg += "\n📉 **Top Potential Fallers (Outbound Momentum)**\n"
    for p in sorted_fallers:
        cost = p["now_cost"] / 10.0
        net_out = p.get("transfers_out_event", 0)
        msg += f"• {p['web_name']} (£{cost}m) — -{net_out:,} transfers\n"

    await update.message.reply_text(msg, parse_mode="Markdown")