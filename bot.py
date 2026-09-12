import os
import logging
import asyncio
import pulp
import requests
from flask import Flask, request, Response
from telegram import Update
from telegram.ext import Application, CommandHandler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

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
        self.user_data_store = {}  # In-memory storage for user FPL IDs

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

    def fetch_live_gwdata(self, gw):
        try:
            response = self.session.get(f"{FPL_BASE_URL}event/{gw}/live/", timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching live data for GW {gw}: {e}")
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

    def _get_3gw_score(self, player, fixtures, start_gw):
        team_id = player['team']
        base_ep = float(player.get('ep_next', 0) or 0)
        form = float(player.get('form', 0) or 0)
        available = self._is_available(player)

        if not available:
            return -50.0 - (3 * 20)

        total_score = 0.0
        for gw_offset in range(3):
            gw = start_gw + gw_offset
            gw_fixtures = [f for f in fixtures if f['event'] == gw and (f['team_h'] == team_id or f['team_a'] == team_id)]
            
            if not gw_fixtures:
                continue
            
            for f in gw_fixtures:
                is_home = (f['team_h'] == team_id)
                fdr = f['team_h_difficulty'] if is_home else f['team_a_difficulty']
                gw_score = (base_ep * 2.0) + (form * 0.5) - (fdr * 0.8)
                total_score += max(gw_score, 0.5)

        return total_score

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

    # Telegram Handlers
    async def start(self, update: Update, context):
        welcome_text = (
            "⚽ **Welcome to the Upgraded 3-GW Horizon FPL Bot!**\n\n"
            "**Core Commands:**\n"
            "• `/setteam <ID>` - Link your FPL Team ID\n"
            "• `/squad` - Optimal starting XI (3-GW Horizon view)\n"
            "• `/freehit` - Generate optimal Free Hit squad\n"
            "• `/transfers` - 3-GW Horizon transfer suggestion\n"
            "• `/live` - Live score & point tracker for current GW\n"
            "• `/hits` - Multi-week net gain analysis for taking a hit (-4)"
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

        self.user_data_store[chat_id] = team_id
        manager = self.fetch_manager_data(team_id)
        if manager:
            name = f"{manager.get('player_first_name', '')} {manager.get('player_last_name', '')}"
            team_name = manager.get('name', 'Unknown Team')
            await update.message.reply_text(f"✅ Successfully linked!\n👤 **Manager:** {name}\n🛡️ **Team:** {team_name}")
        else:
            await update.message.reply_text("⚠️ Team ID saved, but could not verify details from FPL API.")

    async def squad(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = self.user_data_store.get(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        await update.message.reply_text("⏳ Evaluating squad across the 3-gameweek horizon...")

        data = self.fetch_bootstrap_static()
        fixtures = self.fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ FPL API or fixtures unreachable.")
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
            score = self._get_3gw_score(p_info, fixtures, target_gw)
            available = self._is_available(p_info)
            pool.append({
                'id': p_info['id'],
                'name': p_info['web_name'],
                'element_type': p_info['element_type'],
                'now_cost': p_info['now_cost'] / 10.0,
                'available': available,
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

        report = [f"⚽ **3-GW Horizon Squad Lineup (GW {target_gw} - {target_gw+2})**\n", "🟢 **STARTING XI:**"]
        for p in starters:
            warn = " ⚠️ [Doubt]" if not p['available'] else ""
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['now_cost']}m) — 3-GW xP: {p['score']:.1f}{warn}")

        report.append("\n🪑 **BENCH:**")
        for idx, p in enumerate(bench, 1):
            report.append(f"{idx}. [{POS_NAME[p['element_type']]}] {p['name']} (£{p['now_cost']}m) — 3-GW xP: {p['score']:.1f}")

        report.append(f"\n⭐ **Captain:** {captain['name']}")
        report.append(f"🥈 **Vice-Captain:** {vice['name']}")
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def free_hit(self, update: Update, context):
        await update.message.reply_text("⚡ Generating 3-GW Optimized Free Hit squad...")
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
            score = self._get_3gw_score(p, fixtures, target_gw)
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

        await update.message.reply_text("🔄 Scanning 3-week fixture blocks for optimal transfers...")

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
            score = self._get_3gw_score(p, fixtures, target_gw)
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
            await update.message.reply_text(f"✅ Your lowest 3-GW rated player ({weakest['name']}) has no affordable upgrades.")
            return

        best = max(candidates, key=lambda p: self._get_3gw_score(p, fixtures, target_gw))
        best_score = self._get_3gw_score(best, fixtures, target_gw)
        gain = best_score - weakest['score']

        if gain <= 1.5:
            await update.message.reply_text(f"✅ Squad looks well-balanced for the next 3 weeks — no transfer clears the gain threshold.")
            return

        remaining_bank = max_budget - (best['now_cost'] / 10.0)
        report = [
            f"🔄 **Horizon Transfer Suggestion (GW {target_gw}-{target_gw+2})**\n",
            f"🔴 **OUT:** [{POS_NAME[weakest['element_type']]}] {weakest['name']} (£{weakest['cost']}m) — 3-GW xP: {weakest['score']:.1f}",
            f"🟢 **IN:** [{POS_NAME[best['element_type']]}] {best['web_name']} (£{best['now_cost']/10.0}m) — 3-GW xP: {best_score:.1f}",
            f"📈 **3-Week Projected Gain:** +{gain:.1f} xP",
            f"💰 **Bank After:** £{remaining_bank:.1f}m",
        ]
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def hits(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = self.user_data_store.get(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        await update.message.reply_text("💡 Running multi-week hit (-4) valuation analysis...")

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
            score = self._get_3gw_score(p, fixtures, target_gw)
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
            await update.message.reply_text(f"⚠️ No affordable upgrades found for your weakest player ({weakest['name']}).")
            return

        best = max(candidates, key=lambda p: self._get_3gw_score(p, fixtures, target_gw))
        best_score = self._get_3gw_score(best, fixtures, target_gw)
        gain = best_score - weakest['score']
        net_gain = gain - 4.0

        if net_gain <= 0:
            await update.message.reply_text(
                f"❌ **Hit Not Recommended (3-GW Horizon)**\n\n"
                f"Replacing **{weakest['name']}** (3-GW xP: {weakest['score']:.1f}) with **{best['web_name']}** "
                f"(3-GW xP: {best_score:.1f}) gives a 3-week gain of +{gain:.1f} xP. "
                f"After accounting for the 4-point deduction, it yields a net gain of {net_gain:.1f} pts. Not worth a hit."
            )
            return

        remaining_bank = max_budget - (best['now_cost'] / 10.0)
        report = [
            f"💡 **Hit Recommendation (3-GW Horizon)**\n",
            f"🔴 **OUT:** [{POS_NAME[weakest['element_type']]}] {weakest['name']} (£{weakest['cost']}m) — 3-GW xP: {weakest['score']:.1f}",
            f"🟢 **IN:** [{POS_NAME[best['element_type']]}] {best['web_name']} (£{best['now_cost']/10.0}m) — 3-GW xP: {best_score:.1f}",
            f"📈 **3-Week Projected Gain:** +{gain:.1f} xP",
            f"⚖️ **Net Gain (after -4 hit):** +{net_gain:.1f} pts",
            f"💰 **Bank After:** £{remaining_bank:.1f}m",
            f"\n*Verdict:* **Worth taking!** The favorable 3-week fixture swing easily outweighs the hit."
        ]
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def live_tracker(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = self.user_data_store.get(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        data = self.fetch_bootstrap_static()
        if not data:
            await update.message.reply_text("❌ API unreachable.")
            return

        current_gw = next((gw['id'] for gw in data['events'] if gw.get('is_current')), None)
        if current_gw is None:
            current_gw = next((gw['id'] for gw in data['events'] if not gw['finished']), data['events'][0]['id'])

        picks_data = self.fetch_manager_gw_picks(team_id, current_gw)
        live_data = self.fetch_live_gwdata(current_gw)
        if not picks_data or not live_data:
            await update.message.reply_text(f"❌ Live point data for Gameweek {current_gw} is unavailable.")
            return

        element_live = {item['id']: item['stats'] for item in live_data['elements']}
        players_dict = {p['id']: p for p in data['elements']}

        total_live_points = 0
        report = [f"🔴 **Live Gameweek {current_gw} Tracker**\n"]

        for pick in picks_data['picks']:
            p_info = players_dict.get(pick['element'], {})
            p_stats = element_live.get(pick['element'], {})
            pts = p_stats.get('total_points', 0)
            multiplier = pick.get('multiplier', 1)
            effective_pts = pts * multiplier

            if pick.get('position', 11) <= 11:
                total_live_points += effective_pts

            cap_label = " (C)" if multiplier == 2 else (" (VC)" if multiplier > 1 else "")
            report.append(f"• {p_info.get('web_name', 'Player')}{cap_label}: {pts} pts (Total: {effective_pts})")

        report.insert(1, f"🏆 **Estimated Live Points:** {total_live_points}\n")
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")


# --- Flask & Webhook Integration Setup ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL") # Render automatically sets this env variable!
PORT = int(os.getenv("PORT", 10000))

app = Flask(__name__)
fpl_bot = FptBotInstance = FPLBot()

# Build Telegram Application
ptb_app = Application.builder().token(TOKEN).updater(None).build()

# Register handlers
ptb_app.add_handler(CommandHandler("start", fpl_bot.start))
ptb_app.add_handler(CommandHandler("setteam", fpl_bot.set_team))
ptb_app.add_handler(CommandHandler("squad", fpl_bot.squad))
ptb_app.add_handler(CommandHandler("freehit", fpl_bot.free_hit))
ptb_app.add_handler(CommandHandler("transfers", fpl_bot.transfers))
ptb_app.add_handler(CommandHandler("hits", fpl_bot.hits))
ptb_app.add_handler(CommandHandler("live", fpl_bot.live_tracker))


@app.route("/", methods=["GET"])
def index():
    return "FPL Bot Webhook Server is active!", 200


@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """Endpoint that receives updates from Telegram via Webhook"""
    if request.headers.get("content-type") == "application/json":
        json_string = request.get_data().decode("utf-8")
        update = Update.de_json(json_string, ptb_app.bot)
        
        # Run the async update processing inside a synchronous Flask route safely
        async def process():
            async with ptb_app:
                await ptb_app.process_update(update)

        asyncio.run(process())
        return "OK", 200
    else:
        return "Invalid content-type", 403


async def setup_webhook():
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/{TOKEN}"
        await ptb_app.bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook successfully set to: {webhook_url}")
    else:
      logger.warning("RENDER_EXTERNAL_URL environment variable not found. Webhook auto-registration skipped.")


if __name__ == "__main__":
    # Initialize PTB and set webhook automatically upon starting
    async def main():
        async with ptb_app:
            await ptb_app.initialize()
            if RENDER_EXTERNAL_URL:
                await ptb_app.bot.set_webhook(url=f"{RENDER_EXTERNAL_URL.rstrip('/')}/{TOKEN}")
            await ptb_app.start()

    # Run initialization before starting flask
    asyncio.run(setup_webhook())
    
    # Start Flask Web Server for Render
    app.run(host="0.0.0.0", port=PORT)
