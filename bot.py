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

    async def _fetch_manager_history(self, team_id):
        def fetch():
            try:
                res = self.session.get(f"{FPL_BASE_URL}entry/{team_id}/history/", timeout=15)
                res.raise_for_status()
                return res.json()
            except Exception as e:
                logger.error(f"Error fetching manager history for team {team_id}: {e}")
                return None
        return await asyncio.to_thread(fetch)

    def _estimate_free_transfers(self, history_data, upto_event):
        """Reconstruct banked free transfers heading into the gameweek after `upto_event`,
        using FPL's accrual rules: +1 FT per gameweek (capped at 5), and Wildcard/Free Hit
        weeks don't touch the bank since they grant unlimited free transfers that week.
        Falls back to 1 (the safe default most managers are near) if history is unavailable."""
        if not history_data or 'current' not in history_data:
            return 1

        chip_events = {c['event']: c['name'] for c in history_data.get('chips', [])}
        ft = 1
        for row in sorted(history_data['current'], key=lambda r: r['event']):
            event = row['event']
            if event > upto_event:
                break
            transfers_made = row.get('event_transfers', 0) or 0
            if chip_events.get(event) in ('wildcard', 'freehit'):
                transfers_made = 0
            if event > 1:
                ft = min(ft + 1, 5)
            ft = max(0, ft - transfers_made)
        return ft

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

    def _is_hard_excluded(self, p):
        """Genuinely out regardless of how far out we're projecting: injured, suspended,
        left the club, etc. 'doubtful' (status 'd') is handled separately since that's a
        near-term fitness call, not a season-long exclusion."""
        status = p.get('status', 'a')
        return status not in ('a', 'd')

    def _is_doubtful(self, p):
        status = p.get('status', 'a')
        if status == 'd':
            return True
        chance = p.get('chance_of_playing_next_round')
        return chance is not None and chance < 100

    def _availability_multiplier(self, p):
        chance = p.get('chance_of_playing_next_round')
        if chance is None:
            return 0.75 if p.get('status') == 'd' else 1.0
        return chance / 100.0

    def _is_available(self, p):
        """'Fully fit' — used for display flags (e.g. the ⚠️ Doubt tag), not for scoring."""
        return not self._is_hard_excluded(p) and not self._is_doubtful(p)

    # --- Centralized xPts Engine ---
    def _calculate_single_gw_xpts(self, player, fixtures, target_gw, apply_doubt_penalty=True):
        team_id = player['team']
        base_ep = float(player.get('ep_next', 0) or 0)
        form = float(player.get('form', 0) or 0)

        if self._is_hard_excluded(player):
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

        # chance_of_playing_next_round is a near-term fitness signal — only apply it when
        # this call represents the immediate upcoming gameweek, not a future one in a horizon.
        if apply_doubt_penalty and self._is_doubtful(player):
            total_gw_score *= self._availability_multiplier(player)

        return total_gw_score

    def _calculate_horizon_xpts(self, player, fixtures, start_gw, horizon=3):
        if self._is_hard_excluded(player):
            return -100.0

        decay_weights = [1.0, 0.85, 0.7, 0.55]
        total_score = 0.0

        for offset in range(horizon):
            gw = start_gw + offset
            weight = decay_weights[offset] if offset < len(decay_weights) else max(0.2, 1.0 - (offset * 0.2))
            # Only the immediate gameweek (offset 0) gets discounted for current doubt status —
            # a player who's 50% for this week is usually assumed fit again a few weeks out.
            gw_score = self._calculate_single_gw_xpts(player, fixtures, gw, apply_doubt_penalty=(offset == 0))
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
    async def _build_transfer_pools(self, team_id, pick_gw, target_gw, fixtures, data, horizon=3):
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
            score = self._calculate_horizon_xpts(p, fixtures, target_gw, horizon=horizon)
            owned_pool.append({
                'id': p['id'], 'name': p['web_name'], 'element_type': p['element_type'],
                'cost': p['now_cost'] / 10.0, 'team': p['team'], 'score': score,
            })
        if not owned_pool:
            return None

        candidates_pool = []
        for p in data['elements']:
            if p['id'] in owned_ids or self._is_hard_excluded(p):
                continue
            score = self._calculate_horizon_xpts(p, fixtures, target_gw, horizon=horizon)
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
            "• `/freehit` - Generate optimal Free Hit squad for the upcoming GW\n"
            "• `/transfers` - Marginal EV transfer suggestions\n"
            "• `/hits` - Multi-week net gain (-4) valuation analysis\n\n"
            "**Chip Planning:**\n"
            "• `/wildcard` - Long-horizon (6-GW) squad rebuild\n"
            "• `/bboost` - Best upcoming week for Bench Boost\n"
            "• `/triplecap` - Best upcoming week for Triple Captain\n\n"
            "**Research:**\n"
            "• `/differentials [max%] [POS]` - Low-ownership picks by xP"
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

        await update.message.reply_text("⚡ Generating optimal Free Hit squad for the upcoming gameweek...")
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

        # Free Hit only lasts a single gameweek, so score purely on the upcoming GW —
        # a multi-week horizon would reward players whose good fixtures happen AFTER
        # the hit has already reverted.
        candidates = []
        for p in data['elements']:
            if self._is_hard_excluded(p):
                continue
            score = self._calculate_single_gw_xpts(p, fixtures, target_gw)
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
            f"⚡ **Free Hit Squad (GW {target_gw})**",
            f"💰 **Cost:** £{total_cost:.1f}m / £{dynamic_budget:.1f}m limit\n",
            "🛡️ **Starting XI:**",
        ]
        for p in sorted(starters, key=lambda x: x['element_type']):
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m) — xP: {p['score']:.1f}")

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

        history_data = await self._fetch_manager_history(team_id)
        free_transfers = self._estimate_free_transfers(history_data, pick_gw)

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

        hit_cost = max(0, transfers_used - free_transfers) * 4
        net_gain = gain - hit_cost
        remaining_bank = pools['dynamic_budget'] - sum(p['cost'] for p in result['final_squad'])

        report = [f"🔄 **Optimal Transfer Plan — up to {n} (GW {target_gw}-{target_gw+2})**\n", "🔴 **OUT:**"]
        for p in sorted(result['out'], key=lambda x: x['element_type']):
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m) — xP: {p['score']:.1f}")

        report.append("\n🟢 **IN:**")
        for p in sorted(result['in'], key=lambda x: x['element_type']):
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m) — xP: {p['score']:.1f}")

        report.append(f"\n📊 **Transfers used:** {transfers_used} ({free_transfers} free transfer(s) banked)")
        report.append(f"📈 **Gross projected gain:** +{gain:.1f} xP")
        report.append(f"💰 **Bank after:** £{remaining_bank:.1f}m")
        if transfers_used > free_transfers:
            report.append(f"⚠️ **Hit cost:** -{hit_cost:.0f} pts ({transfers_used - free_transfers} paid transfer(s))")
            report.append(f"📉 **Net gain after hits:** +{net_gain:.1f} xP")
            report.append("\nℹ️ _Use `/hits` to weigh whether the extra moves are worth it over a longer horizon._")

        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def _send_transfer_plan_report(self, update, target_gw, horizon, free_transfers, label, result, transfers_used, gain, hit_cost, net_gain):
        report = [f"💡 **{label} — {transfers_used} transfer(s) (GW {target_gw}-{target_gw + horizon - 1})**\n", "🔴 **OUT:**"]
        for p in sorted(result['out'], key=lambda x: x['element_type']):
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m) — xP: {p['score']:.1f}")

        report.append("\n🟢 **IN:**")
        for p in sorted(result['in'], key=lambda x: x['element_type']):
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m) — xP: {p['score']:.1f}")

        report.append(f"\n📈 **Gross gain:** +{gain:.1f} xP")
        report.append(f"🎟️ **Free transfers banked:** {free_transfers}")
        if transfers_used > free_transfers:
            report.append(f"⚠️ **Hit cost:** -{hit_cost:.0f} pts ({transfers_used - free_transfers} paid transfer(s))")
            report.append(f"📉 **Net gain:** +{net_gain:.1f} xP")
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

        history_data = await self._fetch_manager_history(team_id)
        free_transfers = self._estimate_free_transfers(history_data, pick_gw)

        # /hits weighs a -4 hit that persists beyond just the coming GW, so it looks
        # further out than /transfers: the coming gameweek plus the next 3 (4 GWs total).
        HORIZON = 4
        pools = await self._build_transfer_pools(team_id, pick_gw, target_gw, fixtures, data, horizon=HORIZON)
        if not pools:
            await update.message.reply_text(f"❌ Could not retrieve your squad for GW {pick_gw}.")
            return

        # Thresholds: transfers within your banked free count just need to clear model
        # noise; every transfer beyond that must independently justify itself with a real
        # margin, since its cost (-4) is guaranteed while its gain is only an expectation.
        FREE_THRESHOLD = 1.0
        HIT_BUFFER = 2.0
        caveat = (f"\n\nℹ️ _Free transfers banked is estimated at {free_transfers} from your transfer "
                  f"history — if a recent chip wasn't detected correctly, check your FPL app's transfer count._")

        if forced_n:
            result = await self._solve_transfers(pools['owned_pool'], pools['candidates_pool'], forced_n, pools['dynamic_budget'])
            if not result or not result['out']:
                await update.message.reply_text(f"✅ No beneficial transfer plan found within {forced_n} transfer(s).")
                return
            transfers_used = len(result['out'])
            gain = sum(p['score'] for p in result['in']) - sum(p['score'] for p in result['out'])
            hit_cost = max(0, transfers_used - free_transfers) * 4
            net_gain = gain - hit_cost
            threshold = FREE_THRESHOLD if transfers_used <= free_transfers else HIT_BUFFER

            if net_gain <= threshold:
                await update.message.reply_text(
                    f"❌ Taking {transfers_used} transfer(s) here isn't recommended — "
                    f"projected net gain (+{net_gain:.1f} xP) doesn't clear the required margin (+{threshold:.1f} xP)."
                    + caveat, parse_mode="Markdown"
                )
                return

            await self._send_transfer_plan_report(update, target_gw, HORIZON, free_transfers, "Recommended Hit Plan", result, transfers_used, gain, hit_cost, net_gain)
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
            hit_cost = max(0, transfers_used - free_transfers) * 4
            net_gain = gain - hit_cost
            scenarios[transfers_used] = {
                "transfers_used": transfers_used, "gain": gain,
                "hit_cost": hit_cost, "net_gain": net_gain, "result": result,
            }

        best_key = 0
        for k in sorted(k for k in scenarios if k > 0):
            threshold = FREE_THRESHOLD if k <= free_transfers else HIT_BUFFER
            if scenarios[k]['net_gain'] > scenarios[best_key]['net_gain'] + threshold:
                best_key = k

        if best_key == 0:
            await update.message.reply_text("✅ Squad is well-optimized — no transfer (free or paid) clears the value threshold this week.")
            return

        best = scenarios[best_key]
        await self._send_transfer_plan_report(
            update, target_gw, HORIZON, free_transfers, "Recommended Hit Plan", best['result'],
            best['transfers_used'], best['gain'], best['hit_cost'], best['net_gain']
        )
        await update.message.reply_text(caveat, parse_mode="Markdown")

    async def wildcard(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = self.db.get_team_id(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        WILDCARD_HORIZON = 6
        await update.message.reply_text(f"🃏 Generating long-horizon Wildcard squad ({WILDCARD_HORIZON}-GW view)...")

        data = await self._fetch_bootstrap_static()
        fixtures = await self._fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)

        picks_data = await self._fetch_manager_gw_picks(team_id, pick_gw)
        if picks_data and 'entry_history' in picks_data:
            squad_value = picks_data['entry_history'].get('value', 1000) / 10.0
            bank = picks_data['entry_history'].get('bank', 0) / 10.0
            dynamic_budget = squad_value + bank
        else:
            dynamic_budget = 100.0

        # Unlike Free Hit, a Wildcard squad has to hold up over many weeks, so score it
        # with the same long-horizon engine used for /hits rather than a single GW.
        candidates = []
        for p in data['elements']:
            if self._is_hard_excluded(p):
                continue
            score = self._calculate_horizon_xpts(p, fixtures, target_gw, horizon=WILDCARD_HORIZON)
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
            await update.message.reply_text("❌ Couldn't build a valid Wildcard squad.")
            return

        starters, bench = await self._solve_best_xi(squad15)
        total_cost = sum(p['cost'] for p in squad15)
        captain = next((p for p in starters if p.get('is_captain')), None)

        report = [
            f"🃏 **Wildcard Squad (GW {target_gw}-{target_gw + WILDCARD_HORIZON - 1})**",
            f"💰 **Cost:** £{total_cost:.1f}m / £{dynamic_budget:.1f}m limit\n",
            "🛡️ **Starting XI:**",
        ]
        for p in sorted(starters, key=lambda x: x['element_type']):
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m) — {WILDCARD_HORIZON}-GW xP: {p['score']:.1f}")

        report.append("\n🪑 **Bench:**")
        for p in bench:
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m)")

        if captain:
            report.append(f"\n⭐ **Captain:** {captain['name']}")
        report.append(f"\nℹ️ _Scored over the next {WILDCARD_HORIZON} gameweeks since a Wildcard is a permanent rebuild, not a one-week hit._")
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def bench_boost(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = self.db.get_team_id(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        await update.message.reply_text("🚀 Checking bench strength across upcoming gameweeks...")

        data = await self._fetch_bootstrap_static()
        fixtures = await self._fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)
        picks_data = await self._fetch_manager_gw_picks(team_id, pick_gw)
        if not picks_data or 'picks' not in picks_data:
            await update.message.reply_text(f"❌ Could not retrieve your squad for GW {pick_gw}.")
            return

        players_dict = {p['id']: p for p in data['elements']}

        # Bench Boost only pays off if your bench actually scores — check the next few
        # weeks rather than just this one, since a good bench week might not be now.
        BB_LOOKAHEAD = 4
        BB_THRESHOLD = 12.0
        week_results = []
        for offset in range(BB_LOOKAHEAD):
            gw = target_gw + offset
            pool = []
            for pick in picks_data['picks']:
                p_info = players_dict.get(pick['element'])
                if not p_info:
                    continue
                score = self._calculate_single_gw_xpts(p_info, fixtures, gw, apply_doubt_penalty=(offset == 0))
                pool.append({
                    'id': p_info['id'], 'name': p_info['web_name'], 'element_type': p_info['element_type'],
                    'now_cost': p_info['now_cost'] / 10.0, 'available': self._is_available(p_info), 'score': score,
                })
            starters, bench = await self._solve_best_xi(pool)
            if not starters:
                continue
            bench_score = sum(p['score'] for p in bench)
            week_results.append({'gw': gw, 'bench_score': bench_score, 'bench': bench})

        if not week_results:
            await update.message.reply_text("❌ Couldn't evaluate your bench.")
            return

        best = max(week_results, key=lambda w: w['bench_score'])

        report = [f"🚀 **Bench Boost Check (GW {target_gw}-{target_gw + BB_LOOKAHEAD - 1})**\n"]
        for w in week_results:
            marker = " ⭐" if w['gw'] == best['gw'] else ""
            report.append(f"• GW{w['gw']}: bench xP {w['bench_score']:.1f}{marker}")

        report.append("")
        if best['bench_score'] >= BB_THRESHOLD:
            report.append(f"✅ **GW{best['gw']}** looks like your best Bench Boost week (bench xP {best['bench_score']:.1f}).")
        else:
            report.append(
                f"⚠️ Your bench doesn't clear a strong Bench Boost bar in the next {BB_LOOKAHEAD} GWs "
                f"(best is GW{best['gw']} at {best['bench_score']:.1f} xP) — consider strengthening bench depth first."
            )

        report.append(f"\n🪑 **Bench for GW{best['gw']}:**")
        for p in sorted(best['bench'], key=lambda x: x['element_type']):
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} — xP: {p['score']:.1f}")

        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def triple_captain(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = self.db.get_team_id(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        await update.message.reply_text("👑 Scanning captaincy ceiling across upcoming gameweeks...")

        data = await self._fetch_bootstrap_static()
        fixtures = await self._fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)
        picks_data = await self._fetch_manager_gw_picks(team_id, pick_gw)
        if not picks_data or 'picks' not in picks_data:
            await update.message.reply_text(f"❌ Could not retrieve your squad for GW {pick_gw}.")
            return

        players_dict = {p['id']: p for p in data['elements']}

        # Triple Captain wants a single explosive week for your best player, not an
        # averaged one — scan single-GW scores rather than the horizon-discounted engine.
        TC_LOOKAHEAD = 4
        week_best = []
        for offset in range(TC_LOOKAHEAD):
            gw = target_gw + offset
            best_player, best_score = None, float('-inf')
            for pick in picks_data['picks']:
                p_info = players_dict.get(pick['element'])
                if not p_info:
                    continue
                score = self._calculate_single_gw_xpts(p_info, fixtures, gw, apply_doubt_penalty=(offset == 0))
                if score > best_score:
                    best_score, best_player = score, p_info
            if best_player:
                week_best.append({'gw': gw, 'name': best_player['web_name'], 'element_type': best_player['element_type'], 'score': best_score})

        if not week_best:
            await update.message.reply_text("❌ Couldn't evaluate captaincy options.")
            return

        top = max(week_best, key=lambda w: w['score'])

        report = [f"👑 **Triple Captain Scan (GW {target_gw}-{target_gw + TC_LOOKAHEAD - 1})**\n"]
        for w in week_best:
            marker = " ⭐" if w['gw'] == top['gw'] else ""
            report.append(f"• GW{w['gw']}: {w['name']} ({POS_NAME[w['element_type']]}) — xP: {w['score']:.1f}{marker}")

        report.append(
            f"\n✅ **Best week to play Triple Captain: GW{top['gw']}** on {top['name']} — "
            f"the extra multiplier is worth roughly +{top['score']:.1f} xP on top of a normal captaincy."
        )
        report.append("\nℹ️ _This only weighs your current squad — a transfer or a newly confirmed double gameweek could change the picture closer to the week._")
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def differentials(self, update: Update, context):
        max_own = 10.0
        pos_filter = None
        pos_map = {'GKP': 1, 'DEF': 2, 'MID': 3, 'FWD': 4}
        if context.args:
            for arg in context.args:
                if arg.upper() in pos_map:
                    pos_filter = pos_map[arg.upper()]
                    continue
                try:
                    max_own = float(arg)
                except ValueError:
                    pass

        await update.message.reply_text(f"🔍 Scanning for differentials under {max_own:.0f}% ownership...")

        data = await self._fetch_bootstrap_static()
        fixtures = await self._fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)

        candidates = []
        for p in data['elements']:
            if self._is_hard_excluded(p):
                continue
            if pos_filter and p['element_type'] != pos_filter:
                continue
            try:
                owned_pct = float(p.get('selected_by_percent', '0') or 0)
            except (TypeError, ValueError):
                owned_pct = 0.0
            if owned_pct > max_own:
                continue
            score = self._calculate_single_gw_xpts(p, fixtures, target_gw)
            if score <= 0:
                continue
            candidates.append({
                'id': p['id'], 'name': p['web_name'], 'element_type': p['element_type'],
                'cost': p['now_cost'] / 10.0, 'owned_pct': owned_pct, 'score': score,
            })

        if not candidates:
            await update.message.reply_text("❌ No differentials found matching those filters.")
            return

        candidates.sort(key=lambda x: x['score'], reverse=True)
        report = [f"🔍 **Top Differentials (GW {target_gw}, <{max_own:.0f}% owned)**\n"]
        for p in candidates[:10]:
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m, {p['owned_pct']:.1f}% owned) — xP: {p['score']:.1f}")

        await update.message.reply_text("\n".join(report), parse_mode="Markdown")


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
    application.add_handler(CommandHandler("wildcard", bot_instance.wildcard))
    application.add_handler(CommandHandler("bboost", bot_instance.bench_boost))
    application.add_handler(CommandHandler("triplecap", bot_instance.triple_captain))
    application.add_handler(CommandHandler("differentials", bot_instance.differentials))

    logger.info("Starting FPL Telegram Bot via Polling...")
    application.run_polling()

# --- Execution Entry Point ---
if __name__ == "__main__":
    web_thread = Thread(target=run_web)
    web_thread.daemon = True
    web_thread.start()
    logger.info("Background Flask health check server started.")

    start_telegram_bot()
