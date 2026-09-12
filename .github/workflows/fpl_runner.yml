import os
import logging
import time
import sqlite3
import asyncio
from threading import Thread
import pulp
import requests
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Flask Server Setup for Render Health Checks ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "FPL Bot is active and healthy!", 200

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
# --------------------------------------------------

FPL_BASE_URL = "https://fantasy.premierleague.com/api/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://fantasy.premierleague.com/"
}

POS_NAME = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


class DatabaseManager:
    def __init__(self, db_path="fpl_bot.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    team_id TEXT NOT NULL
                )
            """)
            conn.commit()

    def get_team_id(self, chat_id):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT team_id FROM users WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
            return row[0] if row else None

    def set_team_id(self, chat_id, team_id):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO users (chat_id, team_id) VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET team_id = ?
            """, (chat_id, team_id, team_id))
            conn.commit()


class FPLBot:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.db = DatabaseManager()

        # Simple TTL Caching (5-minute expiration)
        self._cache = {
            "bootstrap": {"data": None, "time": 0},
            "fixtures": {"data": None, "time": 0}
        }
        self.cache_ttl = 300  # 5 minutes

    async def _fetch_bootstrap_static(self):
        now = time.time()
        if self._cache["bootstrap"]["data"] and (now - self._cache["bootstrap"]["time"] < self.cache_ttl):
            return self._cache["bootstrap"]["data"]

        def fetch():
            try:
                res = self.session.get(f"{FPL_BASE_URL}bootstrap-static/", timeout=15)
                res.raise_for_status()
                return res.json()
            except Exception as e:
                logger.error(f"Error fetching bootstrap-static: {e}")
                return None

        data = await asyncio.to_thread(fetch)
        if data:
            self._cache["bootstrap"] = {"data": data, "time": now}
        return data

    async def _fetch_fixtures(self):
        now = time.time()
        if self._cache["fixtures"]["data"] and (now - self._cache["fixtures"]["time"] < self.cache_ttl):
            return self._cache["fixtures"]["data"]

        def fetch():
            try:
                res = self.session.get(f"{FPL_BASE_URL}fixtures/", timeout=15)
                res.raise_for_status()
                return res.json()
            except Exception as e:
                logger.error(f"Error fetching fixtures: {e}")
                return None

        data = await asyncio.to_thread(fetch)
        if data:
            self._cache["fixtures"] = {"data": data, "time": now}
        return data

    async def _fetch_manager_data(self, team_id):
        def fetch():
            try:
                res = self.session.get(f"{FPL_BASE_URL}entry/{team_id}/", timeout=15)
                res.raise_for_status()
                return res.json()
            except Exception as e:
                logger.error(f"Error fetching manager data for ID {team_id}: {e}")
                return None
        return await asyncio.to_thread(fetch)

    async def _fetch_manager_gw_picks(self, team_id, gw):
        def fetch():
            try:
                res = self.session.get(f"{FPL_BASE_URL}entry/{team_id}/event/{gw}/picks/", timeout=15)
                res.raise_for_status()
                return res.json()
            except Exception as e:
                logger.error(f"Error fetching manager GW picks for team {team_id} GW {gw}: {e}")
                return None
        return await asyncio.to_thread(fetch)

    def _get_target_and_pick_gw(self, data):
        events = data['events']
        next_gw = next((gw['id'] for gw in events if gw.get('is_next')), None)
        current_gw = next((gw['id'] for gw in events if gw.get('is_current')), None)

        if next_gw is not None:
            pick_gw = max(1, next_gw - 1) if current_gw is None else current_gw
            target_gw = next_gw
        elif current_gw is not None:
            pick_gw = current_gw
            target_gw = current_gw
        else:
            pick_gw = events[-1]['id']
            target_gw = pick_gw

        return target_gw, pick_gw

    def _is_available(self, p):
        status = p.get('status', 'a')
        chance = p.get('chance_of_playing_next_round', 100)
        chance = 100 if chance is None else chance
        return status == 'a' and chance >= 75

    # --- Centralized xPts Engine ---
    def _calculate_single_gw_xpts(self, player, fixtures, target_gw):
        team_id = player['team']
        base_ep = float(player.get('ep_next', 0) or 0)
        form = float(player.get('form', 0) or 0)

        if not self._is_available(player):
            return -50.0

        gw_fixtures = [f for f in fixtures if f['event'] == target_gw and (f['team_h'] == team_id or f['team_a'] == team_id)]

        # Blank gameweeks yield 0 points — the player has no fixture to score in
        if not gw_fixtures:
            return 0.0

        total_gw_score = 0.0
        for f in gw_fixtures:
            is_home = (f['team_h'] == team_id)
            fdr = f['team_h_difficulty'] if is_home else f['team_a_difficulty']
            gw_score = (base_ep * 1.8) + (form * 0.4) - (fdr * 0.7)
            total_gw_score += max(gw_score, 0.5)

        return total_gw_score

    def _calculate_horizon_xpts(self, player, fixtures, start_gw, horizon=3):
        if not self._is_available(player):
            return -100.0

        decay_weights = [1.0, 0.8, 0.6]
        total_score = 0.0

        for offset in range(horizon):
            gw = start_gw + offset
            weight = decay_weights[offset] if offset < len(decay_weights) else max(0.2, 1.0 - (offset * 0.2))
            gw_score = self._calculate_single_gw_xpts(player, fixtures, gw)
            if gw_score < -10:
                return -100.0
            total_score += (gw_score * weight)

        return total_score

    # --- ILP Solvers ---
    async def _solve_best_xi(self, pool):
        def solve():
            if len(pool) < 11:
                return [], []

            prob = pulp.LpProblem("XI", pulp.LpMaximize)
            start = {p['id']: pulp.LpVariable(f"s_{p['id']}", cat="Binary") for p in pool}
            cap = {p['id']: pulp.LpVariable(f"c_{p['id']}", cat="Binary") for p in pool}

            prob += pulp.lpSum(p['score'] * start[p['id']] for p in pool) + \
                    pulp.lpSum(p['score'] * cap[p['id']] for p in pool)

            prob += pulp.lpSum(start.values()) == 11
            prob += pulp.lpSum(cap.values()) == 1
            for p in pool:
                prob += cap[p['id']] <= start[p['id']]

            for pos, lo, hi in [(1, 1, 1), (2, 3, 5), (3, 2, 5), (4, 1, 3)]:
                members = [p for p in pool if p['element_type'] == pos]
                prob += pulp.lpSum(start[p['id']] for p in members) >= lo
                prob += pulp.lpSum(start[p['id']] for p in members) <= hi

            status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
            if pulp.LpStatus[status] != "Optimal":
                return [], []

            starters, bench = [], []
            for p in pool:
                p['is_captain'] = pulp.value(cap[p['id']]) == 1
                (starters if pulp.value(start[p['id']]) == 1 else bench).append(p)

            non_cap = sorted([p for p in starters if not p['is_captain']], key=lambda x: x['score'], reverse=True)
            vc_id = non_cap[0]['id'] if non_cap else None
            for p in starters:
                p['is_vice'] = p['id'] == vc_id

            bench_gk = [p for p in bench if p['element_type'] == 1]
            bench_out = sorted([p for p in bench if p['element_type'] != 1], key=lambda x: x['score'], reverse=True)
            return starters, bench_gk + bench_out

        return await asyncio.to_thread(solve)

    async def _solve_best_15(self, candidates, budget=100.0):
        def solve():
            prob = pulp.LpProblem("Squad15", pulp.LpMaximize)
            pick = {p['id']: pulp.LpVariable(f"p_{p['id']}", cat="Binary") for p in candidates}

            prob += pulp.lpSum(p['score'] * pick[p['id']] for p in candidates)
            prob += pulp.lpSum(p['cost'] * pick[p['id']] for p in candidates) <= budget
            prob += pulp.lpSum(pick.values()) == 15

            for pos, count in [(1, 2), (2, 5), (3, 5), (4, 3)]:
                members = [p for p in candidates if p['element_type'] == pos]
                prob += pulp.lpSum(pick[p['id']] for p in members) == count

            teams = {p['team'] for p in candidates}
            for team_id in teams:
                members = [p for p in candidates if p['team'] == team_id]
                prob += pulp.lpSum(pick[p['id']] for p in members) <= 3

            status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
            if pulp.LpStatus[status] != "Optimal":
                return []
            return [p for p in candidates if pulp.value(pick[p['id']]) == 1]

        return await asyncio.to_thread(solve)

    # --- Shared Squad/Candidate Pool Builder (uses pick_gw for the real squad, target_gw for horizon scoring) ---
    async def _build_transfer_pools(self, team_id, pick_gw, target_gw, fixtures, data):
        picks_data = await self._fetch_manager_gw_picks(team_id, pick_gw)
        if not picks_data or 'picks' not in picks_data:
            return None

        players_dict = {p['id']: p for p in data['elements']}
        bank = picks_data.get('entry_history', {}).get('bank', 0) / 10.0
        squad_value = picks_data.get('entry_history', {}).get('value', 1000) / 10.0
        dynamic_budget = squad_value + bank
        owned_ids = {pick['element'] for pick in picks_data['picks']}

        owned_pool = []
        for pick in picks_data['picks']:
            p = players_dict.get(pick['element'])
            if not p:
                continue
            score = self._calculate_horizon_xpts(p, fixtures, target_gw, horizon=3)
            owned_pool.append({
                'id': p['id'], 'name': p['web_name'], 'element_type': p['element_type'],
                'cost': p['now_cost'] / 10.0, 'team': p['team'], 'score': score,
            })
        if not owned_pool:
            return None

        candidates_pool = []
        for p in data['elements']:
            if p['id'] in owned_ids or not self._is_available(p):
                continue
            score = self._calculate_horizon_xpts(p, fixtures, target_gw, horizon=3)
            candidates_pool.append({
                'id': p['id'], 'name': p['web_name'], 'element_type': p['element_type'],
                'cost': p['now_cost'] / 10.0, 'team': p['team'], 'score': score,
            })

        return {
            "owned_pool": owned_pool,
            "candidates_pool": candidates_pool,
            "dynamic_budget": dynamic_budget,
        }

    # --- Multi-Transfer ILP Solver: finds the best squad reachable within `max_transfers` changes ---
    async def _solve_transfers(self, owned_pool, candidates_pool, max_transfers, budget):
        def solve():
            owned_ids = {p['id'] for p in owned_pool}
            universe = {p['id']: p for p in owned_pool}
            for p in candidates_pool:
                universe[p['id']] = p
            players = list(universe.values())

            prob = pulp.LpProblem("Transfers", pulp.LpMaximize)
            x = {p['id']: pulp.LpVariable(f"x_{p['id']}", cat="Binary") for p in players}

            prob += pulp.lpSum(p['score'] * x[p['id']] for p in players)
            prob += pulp.lpSum(x.values()) == 15
            prob += pulp.lpSum(p['cost'] * x[p['id']] for p in players) <= budget

            for pos, count in [(1, 2), (2, 5), (3, 5), (4, 3)]:
                members = [p for p in players if p['element_type'] == pos]
                prob += pulp.lpSum(x[p['id']] for p in members) == count

            teams = {p['team'] for p in players}
            for team_id in teams:
                members = [p for p in players if p['team'] == team_id]
                prob += pulp.lpSum(x[p['id']] for p in members) <= 3

            # At most `max_transfers` of the currently owned players may be dropped
            prob += pulp.lpSum(x[pid] for pid in owned_ids if pid in x) >= (15 - max_transfers)

            status = prob.solve(pulp.PULP_CBC_CMD(msg=0))
            if pulp.LpStatus[status] != "Optimal":
                return None

            final_squad = [p for p in players if pulp.value(x[p['id']]) == 1]
            final_ids = {p['id'] for p in final_squad}
            return {
                "final_squad": final_squad,
                "out": [p for p in owned_pool if p['id'] not in final_ids],
                "in": [p for p in final_squad if p['id'] not in owned_ids],
            }
        return await asyncio.to_thread(solve)

    # --- Telegram Handlers ---
    async def start(self, update: Update, context):
        welcome_text = (
            "⚽ **Welcome to the FPL Assistant Bot!**\n\n"
            "**Core Commands:**\n"
            "• `/setteam <ID>` - Link your FPL Team ID\n"
            "• `/squad` - Optimal starting XI for the immediate GW\n"
            "• `/freehit` - Generate 3-GW Horizon Free Hit squad\n"
            "• `/transfers` - Marginal EV transfer suggestions\n"
            "• `/hits` - Multi-week net gain (-4) valuation analysis"
        )
        await update.message.reply_text(welcome_text, parse_mode="Markdown")

    async def set_team(self, update: Update, context):
        chat_id = update.effective_chat.id
        if not context.args:
            await update.message.reply_text("⚠️ Please provide your FPL Team ID. Example: `/setteam 1234567`", parse_mode="Markdown")
            return

        team_id = context.args[0]
        if not team_id.isdigit():
            await update.message.reply_text("❌ Invalid Team ID format. It should be a number.")
            return

        manager = await self._fetch_manager_data(team_id)
        if manager:
            self.db.set_team_id(chat_id, team_id)
            name = f"{manager.get('player_first_name', '')} {manager.get('player_last_name', '')}"
            team_name = manager.get('name', 'Unknown Team')
            await update.message.reply_text(f"✅ Successfully linked!\n👤 **Manager:** {name}\n🛡️ **Team:** {team_name}")
        else:
            await update.message.reply_text("❌ Could not verify Team ID from FPL API. Please check and try again.")

    async def squad(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = self.db.get_team_id(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        await update.message.reply_text("⏳ Evaluating optimal starting XI for the upcoming gameweek...")

        data = await self._fetch_bootstrap_static()
        fixtures = await self._fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ FPL API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)
        picks_data = await self._fetch_manager_gw_picks(team_id, pick_gw)
        if not picks_data or 'picks' not in picks_data:
            await update.message.reply_text(f"❌ Could not retrieve your squad for GW {pick_gw}.")
            return

        players_dict = {p['id']: p for p in data['elements']}

        pool = []
        for pick in picks_data['picks']:
            p_info = players_dict.get(pick['element'])
            if not p_info:
                continue
            score = self._calculate_single_gw_xpts(p_info, fixtures, target_gw)
            pool.append({
                'id': p_info['id'],
                'name': p_info['web_name'],
                'element_type': p_info['element_type'],
                'now_cost': p_info['now_cost'] / 10.0,
                'available': self._is_available(p_info),
                'score': score,
            })

        starters, bench = await self._solve_best_xi(pool)
        if not starters:
            await update.message.reply_text("❌ Couldn't build a valid Starting XI.")
            return

        pos_order = {1: 1, 2: 2, 3: 3, 4: 4}
        starters.sort(key=lambda x: (pos_order[x['element_type']], -x['score']))
        captain = next((p for p in starters if p.get('is_captain')), starters[0])
        vice = next((p for p in starters if p.get('is_vice')), starters[1] if len(starters) > 1 else starters[0])

        report = [f"⚽ **Optimal Starting XI (GW {target_gw})**\n", "🟢 **STARTING XI:**"]
        for p in starters:
            warn = " ⚠️ [Doubt]" if not p['available'] else ""
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['now_cost']}m) — xP: {p['score']:.1f}{warn}")

        report.append("\n🪑 **BENCH:**")
        for idx, p in enumerate(bench, 1):
            report.append(f"{idx}. [{POS_NAME[p['element_type']]}] {p['name']} (£{p['now_cost']}m) — xP: {p['score']:.1f}")

        report.append(f"\n⭐ **Captain:** {captain['name']}")
        report.append(f"🥈 **Vice-Captain:** {vice['name']}")
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def free_hit(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = self.db.get_team_id(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        await update.message.reply_text("⚡ Generating 3-GW Horizon Optimized Free Hit squad...")
        data = await self._fetch_bootstrap_static()
        fixtures = await self._fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)

        # Use actual manager squad value + bank to compute real budget limit
        picks_data = await self._fetch_manager_gw_picks(team_id, pick_gw)
        if picks_data and 'entry_history' in picks_data:
            squad_value = picks_data['entry_history'].get('value', 1000) / 10.0
            bank = picks_data['entry_history'].get('bank', 0) / 10.0
            dynamic_budget = squad_value + bank
        else:
            dynamic_budget = 100.0  # Fallback

        candidates = []
        for p in data['elements']:
            if not self._is_available(p):
                continue
            score = self._calculate_horizon_xpts(p, fixtures, target_gw, horizon=3)
            candidates.append({
                'id': p['id'],
                'name': p['web_name'],
                'element_type': p['element_type'],
                'cost': p['now_cost'] / 10.0,
                'team': p['team'],
                'score': score,
            })

        squad15 = await self._solve_best_15(candidates, budget=dynamic_budget)
        if not squad15:
            await update.message.reply_text("❌ Couldn't build a valid Free Hit squad.")
            return

        starters, bench = await self._solve_best_xi(squad15)
        total_cost = sum(p['cost'] for p in squad15)
        captain = next((p for p in starters if p.get('is_captain')), None)

        report = [
            f"⚡ **Free Hit Horizon Squad (GW {target_gw}-{target_gw+2})**",
            f"💰 **Cost:** £{total_cost:.1f}m / £{dynamic_budget:.1f}m limit\n",
            "🛡️ **Starting XI:**",
        ]
        for p in sorted(starters, key=lambda x: x['element_type']):
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m) — 3-GW xP: {p['score']:.1f}")

        report.append("\n🪑 **Bench:**")
        for p in bench:
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m)")

        if captain:
            report.append(f"\n⭐ **Captain:** {captain['name']}")
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def transfers(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = self.db.get_team_id(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        n = 1
        if context.args:
            try:
                n = max(1, min(int(context.args[0]), 4))
            except ValueError:
                await update.message.reply_text("⚠️ Usage: `/transfers` or `/transfers <1-4>`", parse_mode="Markdown")
                return

        await update.message.reply_text(f"🔄 Analyzing optimal transfer plan (up to {n})...")

        data = await self._fetch_bootstrap_static()
        fixtures = await self._fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ FPL API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)
        pools = await self._build_transfer_pools(team_id, pick_gw, target_gw, fixtures, data)
        if not pools:
            await update.message.reply_text(f"❌ Could not retrieve your squad for GW {pick_gw}.")
            return

        result = await self._solve_transfers(pools['owned_pool'], pools['candidates_pool'], n, pools['dynamic_budget'])
        if not result or not result['out']:
            await update.message.reply_text("✅ Squad is well-optimized — no beneficial transfer found within your requested limit.")
            return

        transfers_used = len(result['out'])
        gain = sum(p['score'] for p in result['in']) - sum(p['score'] for p in result['out'])

        # Guard against recommending a change that's within model noise —
        # require a minimum gain per transfer used, scaled by count.
        min_gain_threshold = 1.0 * transfers_used
        if gain < min_gain_threshold:
            await update.message.reply_text("✅ Squad is well-optimized — no beneficial transfer found within your requested limit.")
            return

        hit_cost = max(0, transfers_used - 1) * 4  # assumes 1 free transfer banked
        net_gain = gain - hit_cost
        remaining_bank = pools['dynamic_budget'] - sum(p['cost'] for p in result['final_squad'])

        report = [f"🔄 **Optimal Transfer Plan — up to {n} (GW {target_gw}-{target_gw+2})**\n", "🔴 **OUT:**"]
        for p in sorted(result['out'], key=lambda x: x['element_type']):
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m) — xP: {p['score']:.1f}")

        report.append("\n🟢 **IN:**")
        for p in sorted(result['in'], key=lambda x: x['element_type']):
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m) — xP: {p['score']:.1f}")

        report.append(f"\n📊 **Transfers used:** {transfers_used}")
        report.append(f"📈 **Gross projected gain:** +{gain:.1f} xP")
        report.append(f"💰 **Bank after:** £{remaining_bank:.1f}m")
        if transfers_used > 1:
            report.append(f"⚠️ **Assumed hit cost:** -{hit_cost:.0f} pts (assumes 1 free transfer banked)")
            report.append(f"📉 **Net gain after hits:** +{net_gain:.1f} xP")
            report.append("\nℹ️ _If you have more than 1 free transfer, use `/hits` to weigh whether the extra moves are worth any remaining hit cost._")

        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def _send_transfer_plan_report(self, update, target_gw, label, result, transfers_used, gain, hit_cost, net_gain):
        report = [f"💡 **{label} — {transfers_used} transfer(s) (GW {target_gw}-{target_gw+2})**\n", "🔴 **OUT:**"]
        for p in sorted(result['out'], key=lambda x: x['element_type']):
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m) — xP: {p['score']:.1f}")

        report.append("\n🟢 **IN:**")
        for p in sorted(result['in'], key=lambda x: x['element_type']):
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m) — xP: {p['score']:.1f}")

        report.append(f"\n📈 **Gross gain:** +{gain:.1f} xP")
        if transfers_used > 1:
            report.append(f"⚠️ **Hit cost:** -{hit_cost:.0f} pts")
            report.append(f"📉 **Net gain:** +{net_gain:.1f} xP")
        report.append("\nℹ️ _Assumes 1 free transfer banked — adjust manually if you have more or fewer._")
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def hits(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = self.db.get_team_id(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        forced_n = None
        if context.args:
            try:
                forced_n = max(1, min(int(context.args[0]), 4))
            except ValueError:
                await update.message.reply_text("⚠️ Usage: `/hits` or `/hits <1-4>`", parse_mode="Markdown")
                return

        await update.message.reply_text("💡 Running net-gain hit valuation analysis...")

        data = await self._fetch_bootstrap_static()
        fixtures = await self._fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ FPL API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)
        pools = await self._build_transfer_pools(team_id, pick_gw, target_gw, fixtures, data)
        if not pools:
            await update.message.reply_text(f"❌ Could not retrieve your squad for GW {pick_gw}.")
            return

        # Thresholds: a free transfer just needs to clear model noise; every additional
        # paid hit must independently justify itself with a real margin, since its cost
        # (-4) is guaranteed while its gain is only an expectation.
        FREE_THRESHOLD = 1.0
        HIT_BUFFER = 2.0
        caveat = ("\n\nℹ️ _Note: assumes 1 free transfer banked. If you already have more "
                  "than 1 saved, some of this \"hit\" cost may not actually apply — check "
                  "your FPL app's transfer count._")

        if forced_n:
            result = await self._solve_transfers(pools['owned_pool'], pools['candidates_pool'], forced_n, pools['dynamic_budget'])
            if not result or not result['out']:
                await update.message.reply_text(f"✅ No beneficial transfer plan found within {forced_n} transfer(s).")
                return
            transfers_used = len(result['out'])
            gain = sum(p['score'] for p in result['in']) - sum(p['score'] for p in result['out'])
            hit_cost = max(0, transfers_used - 1) * 4
            net_gain = gain - hit_cost
            threshold = FREE_THRESHOLD if transfers_used <= 1 else HIT_BUFFER

            if net_gain <= threshold:
                await update.message.reply_text(
                    f"❌ Taking {transfers_used} transfer(s) here isn't recommended — "
                    f"projected net gain (+{net_gain:.1f} xP) doesn't clear the required margin (+{threshold:.1f} xP)."
                    + caveat, parse_mode="Markdown"
                )
                return

            await self._send_transfer_plan_report(update, target_gw, "Recommended Hit Plan", result, transfers_used, gain, hit_cost, net_gain)
            return

        # No count specified: explore 1–4 transfers and build a staircase of accepted upgrades,
        # where each additional hit must clear its own marginal buffer over the current best plan.
        scenarios = {0: {"transfers_used": 0, "gain": 0.0, "hit_cost": 0.0, "net_gain": 0.0, "result": None}}
        for t in range(1, 5):
            result = await self._solve_transfers(pools['owned_pool'], pools['candidates_pool'], t, pools['dynamic_budget'])
            if not result or not result['out']:
                continue
            transfers_used = len(result['out'])
            gain = sum(p['score'] for p in result['in']) - sum(p['score'] for p in result['out'])
            hit_cost = max(0, transfers_used - 1) * 4
            net_gain = gain - hit_cost
            scenarios[transfers_used] = {
                "transfers_used": transfers_used, "gain": gain,
                "hit_cost": hit_cost, "net_gain": net_gain, "result": result,
            }

        best_key = 0
        for k in sorted(k for k in scenarios if k > 0):
            threshold = FREE_THRESHOLD if k == 1 else HIT_BUFFER
            if scenarios[k]['net_gain'] > scenarios[best_key]['net_gain'] + threshold:
                best_key = k

        if best_key == 0:
            await update.message.reply_text("✅ Squad is well-optimized — no transfer (free or paid) clears the value threshold this week.")
            return

        best = scenarios[best_key]
        await self._send_transfer_plan_report(
            update, target_gw, "Recommended Hit Plan", best['result'],
            best['transfers_used'], best['gain'], best['hit_cost'], best['net_gain']
        )
        await update.message.reply_text(caveat, parse_mode="Markdown")


def start_telegram_bot():
    BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN environment variable is missing!")
        return

    bot_instance = FPLBot()
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", bot_instance.start))
    application.add_handler(CommandHandler("setteam", bot_instance.set_team))
    application.add_handler(CommandHandler("squad", bot_instance.squad))
    application.add_handler(CommandHandler("freehit", bot_instance.free_hit))
    application.add_handler(CommandHandler("transfers", bot_instance.transfers))
    application.add_handler(CommandHandler("hits", bot_instance.hits))

    logger.info("Starting FPL Telegram Bot via Polling...")
    application.run_polling()

# --- Execution Entry Point ---
if __name__ == "__main__":
    web_thread = Thread(target=run_web)
    web_thread.daemon = True
    web_thread.start()
    logger.info("Background Flask health check server started.")

    start_telegram_bot()
