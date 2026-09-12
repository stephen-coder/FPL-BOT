import os
import logging
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

class FPLBot:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.user_data_store = {}

    def fetch_bootstrap_static(self):
        try:
            response = self.session.get(f"{FPL_BASE_URL}bootstrap-static/", timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching bootstrap-static: {e}")
            return None

    def fetch_fixtures(self):
        try:
            response = self.session.get(f"{FPL_BASE_URL}fixtures/", timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching fixtures: {e}")
            return None

    def fetch_manager_data(self, team_id):
        try:
            response = self.session.get(f"{FPL_BASE_URL}entry/{team_id}/", timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching manager data for ID {team_id}: {e}")
            return None

    def fetch_manager_gw_picks(self, team_id, gw):
        try:
            response = self.session.get(f"{FPL_BASE_URL}entry/{team_id}/event/{gw}/picks/", timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching manager GW picks for team {team_id} GW {gw}: {e}")
            return None

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
        if not gw_fixtures:
            return max(base_ep, 2.0)
        
        total_gw_score = 0.0
        for f in gw_fixtures:
            is_home = (f['team_h'] == team_id)
            fdr = f['team_h_difficulty'] if is_home else f['team_a_difficulty']
            # Blended heuristic model: ep_next weight + form weight - FDR penalty
            gw_score = (base_ep * 1.8) + (form * 0.4) - (fdr * 0.7)
            total_gw_score += max(gw_score, 0.5)

        return total_gw_score

    def _calculate_horizon_xpts(self, player, fixtures, start_gw, horizon=3):
        if not self._is_available(player):
            return -100.0

        decay_weights = [1.0, 0.8, 0.6]  # Uncertainty decay across horizon weeks
        total_score = 0.0

        for offset in range(horizon):
            gw = start_gw + offset
            weight = decay_weights[offset] if offset < len(decay_weights) else 0.5
            gw_score = self._calculate_single_gw_xpts(player, fixtures, gw)
            if gw_score < -10:  # Unavailable flag penalty
                return -100.0
            total_score += (gw_score * weight)

        return total_score

    # --- ILP Solvers ---
    def _solve_best_xi(self, pool):
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

    def _solve_best_15(self, candidates, budget=100.0):
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

        manager = self.fetch_manager_data(team_id)
        if manager:
            self.user_data_store[chat_id] = team_id
            name = f"{manager.get('player_first_name', '')} {manager.get('player_last_name', '')}"
            team_name = manager.get('name', 'Unknown Team')
            await update.message.reply_text(f"✅ Successfully linked!\n👤 **Manager:** {name}\n🛡️ **Team:** {team_name}")
        else:
            await update.message.reply_text("❌ Could not verify Team ID from FPL API. Please check and try again.")

    async def squad(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = self.user_data_store.get(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        await update.message.reply_text("⏳ Evaluating optimal starting XI for the upcoming gameweek...")

        data = self.fetch_bootstrap_static()
        fixtures = self.fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ FPL API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)
        picks_data = self.fetch_manager_gw_picks(team_id, pick_gw)
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

        starters, bench = self._solve_best_xi(pool)
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
        await update.message.reply_text("⚡ Generating 3-GW Horizon Optimized Free Hit squad...")
        data = self.fetch_bootstrap_static()
        fixtures = self.fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ API unreachable.")
            return

        target_gw, _ = self._get_target_and_pick_gw(data)

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

        squad15 = self._solve_best_15(candidates, budget=100.0)
        if not squad15:
            await update.message.reply_text("❌ Couldn't build a valid Free Hit squad.")
            return

        starters, bench = self._solve_best_xi(squad15)
        total_cost = sum(p['cost'] for p in squad15)
        captain = next((p for p in starters if p.get('is_captain')), None)

        report = [
            f"⚡ **Free Hit Horizon Squad (GW {target_gw}-{target_gw+2})**",
            f"💰 **Cost:** £{total_cost:.1f}m / £100.0m\n",
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
        team_id = self.user_data_store.get(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        await update.message.reply_text("🔄 Analyzing marginal EV for transfer options...")

        data = self.fetch_bootstrap_static()
        fixtures = self.fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ FPL API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)
        picks_data = self.fetch_manager_gw_picks(team_id, pick_gw)
        if not picks_data or 'picks' not in picks_data:
            await update.message.reply_text(f"❌ Could not retrieve your squad for GW {pick_gw}.")
            return

        players_dict = {p['id']: p for p in data['elements']}
        bank = picks_data.get('entry_history', {}).get('bank', 0) / 10.0
        owned_ids = {pick['element'] for pick in picks_data['picks']}

        owned = []
        for pick in picks_data['picks']:
            p = players_dict.get(pick['element'])
            if not p:
                continue
            score = self._calculate_horizon_xpts(p, fixtures, target_gw, horizon=3)
            owned.append({
                'id': p['id'], 'name': p['web_name'], 'element_type': p['element_type'],
                'cost': p['now_cost'] / 10.0, 'score': score,
            })

        if not owned:
            await update.message.reply_text("❌ Could not parse owned player elements.")
            return

        weakest = min(owned, key=lambda x: x['score'])
        max_budget = weakest['cost'] + bank

        candidates = [
            p for p in data['elements']
            if p['id'] not in owned_ids and p['element_type'] == weakest['element_type']
            and self._is_available(p) and (p['now_cost'] / 10.0) <= max_budget
        ]
        if not candidates:
            await update.message.reply_text(f"✅ Your lowest rated player ({weakest['name']}) has no affordable upgrades.")
            return

        best = max(candidates, key=lambda p: self._calculate_horizon_xpts(p, fixtures, target_gw, horizon=3))
        best_score = self._calculate_horizon_xpts(best, fixtures, target_gw, horizon=3)
        gain = best_score - weakest['score']

        if gain <= 1.5:
            await update.message.reply_text(f"✅ Squad is well-optimized — no transfer clears the marginal gain threshold.")
            return

        remaining_bank = max_budget - (best['now_cost'] / 10.0)
        report = [
            f"🔄 **Marginal Transfer Suggestion (GW {target_gw}-{target_gw+2})**\n",
            f"🔴 **OUT:** [{POS_NAME[weakest['element_type']]}] {weakest['name']} (£{weakest['cost']}m) — 3-GW xP: {weakest['score']:.1f}",
            f"🟢 **IN:** [{POS_NAME[best['element_type']]}] {best['web_name']} (£{best['now_cost']/10.0}m) — 3-GW xP: {best_score:.1f}",
            f"📈 **Projected Marginal Gain:** +{gain:.1f} xP",
            f"💰 **Bank After:** £{remaining_bank:.1f}m",
        ]
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def hits(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = self.user_data_store.get(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        await update.message.reply_text("💡 Running net-gain hit valuation analysis with safety buffer...")

        data = self.fetch_bootstrap_static()
        fixtures = self.fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ FPL API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)
        picks_data = self.fetch_manager_gw_picks(team_id, pick_gw)
        if not picks_data or 'picks' not in picks_data:
            await update.message.reply_text(f"❌ Could not retrieve your squad for GW {pick_gw}.")
            return

        players_dict = {p['id']: p for p in data['elements']}
        bank = picks_data.get('entry_history', {}).get('bank', 0) / 10.0
        owned_ids = {pick['element'] for pick in picks_data['picks']}

        owned = []
        for pick in picks_data['picks']:
            p = players_dict.get(pick['element'])
            if not p:
                continue
            score = self._calculate_horizon_xpts(p, fixtures, target_gw, horizon=3)
            owned.append({
                'id': p['id'], 'name': p['web_name'], 'element_type': p['element_type'],
                'cost': p['now_cost'] / 10.0, 'score': score,
            })

        if not owned:
            await update.message.reply_text("❌ Could not parse owned player elements.")
            return

        weakest = min(owned, key=lambda x: x['score'])
        max_budget = weakest['cost'] + bank

        candidates = [
            p for p in data['elements']
            if p['id'] not in owned_ids and p['element_type'] == weakest['element_type']
            and self._is_available(p) and (p['now_cost'] / 10.0) <= max_budget
        ]
        if not candidates:
            await update.message.reply_text(f"✅ Your lowest rated player ({weakest['name']}) has no upgrade candidates.")
            return

        best = max(candidates, key=lambda p: self._calculate_horizon_xpts(p, fixtures, target_gw, horizon=3))
        best_score = self._calculate_horizon_xpts(best, fixtures, target_gw, horizon=3)
        gain = best_score - weakest['score']
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
    # Local testing fallback
    web_thread = Thread(target=run_web)
    web_thread.daemon = True
    web_thread.start()
    logger.info("Background Flask health check server started.")
    
    start_telegram_bot()
