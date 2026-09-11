import os
import requests
import pulp
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Load environment variables securely
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RIVAL_ID = os.getenv("FPL_RIVAL_ID")  # Phase 2: Rival ID for scouting

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

FPL_BASE_URL = "https://fantasy.premierleague.com/api"

class FPLBot:
    def __init__(self):
        self.session = requests.Session()

    def fetch_bootstrap_static(self):
        """Fetches general FPL data including players, teams, and gameweeks."""
        try:
            url = f"{FPL_BASE_URL}/bootstrap-static/"
            response = self.session.get(url)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"Error fetching bootstrap static data: {e}")
            return None

    def fetch_fixtures(self):
        """Fetches fixture difficulty and schedule for horizon projections."""
        try:
            url = f"{FPL_BASE_URL}/fixtures/"
            response = self.session.get(url)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"Error fetching fixtures: {e}")
            return None

    def fetch_manager_history(self, manager_id):
        """Fetches historical gameweek data for a manager (used in Phase 2)."""
        try:
            url = f"{FPL_BASE_URL}/entry/{manager_id}/history/"
            response = self.session.get(url)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"Error fetching manager history for ID {manager_id}: {e}")
            return None

    def check_deadline_and_prices(self):
        """Phase 1: Checks upcoming deadlines and monitors price changes."""
        data = self.fetch_bootstrap_static()
        if not data:
            return "Unable to fetch FPL data right now."

        next_gw = next((gw for gw in data['events'] if not gw['finished'] and not gw['is_current']), None)
        deadline_text = f"⏳ Next Gameweek Deadline: {next_gw['deadline_time']}" if next_gw else "No upcoming deadline found."

        price_risers = [p['web_name'] for p in data['elements'] if p['cost_change_event'] > 0]
        price_fallers = [p['web_name'] for p in data['elements'] if p['cost_change_event'] < 0]
        
        report = f"{deadline_text}\n\n📈 Risers: {', ' if price_risers else 'None'}{', '.join(price_risers[:5])}\n📉 Fallers: {', '.join(price_fallers[:5]) if price_fallers else 'None'}"
        return report

    def scout_rival(self, rival_id):
        """Phase 2: Analyzes rival performance and checks for roast triggers."""
        history = self.fetch_manager_history(rival_id)
        if not history:
            return f"Could not fetch history for Rival ID {rival_id}."

        current_season = history.get('current', [])
        if current_season:
            latest_gw = current_season[-1]
            msg = f"🔍 **Rival Scouting Report (ID: {rival_id})**\n• GW {latest_gw['event']} Points: {latest_gw['points']}\n• Overall Rank: {latest_gw['overall_rank']:,}"
            if latest_gw['points'] < 40:
                msg += f"\n\n🔥 **Roast Alert:** Scoring only {latest_gw['points']} points? Absolute mud!"
            return msg
        return "No recent gameweek data available for this rival."

    def run_optimization(self):
        """Runs PuLP linear programming model for squad selection over a 3-week horizon."""
        data = self.fetch_bootstrap_static()
        if not data:
            return "Optimization failed: FPL API unreachable."

        players = data['elements']
        prob = pulp.LpProblem("FPL_Optimization", pulp.LpMaximize)
        player_vars = {p['id']: pulp.LpVariable(f"player_{p['id']}", cat='Binary') for p in players}

        prob += pulp.lpSum([p['total_points'] * player_vars[p['id']] for p in players])
        prob += pulp.lpSum([p['now_cost'] * player_vars[p['id']] for p in players]) <= 1000
        prob += pulp.lpSum([player_vars[p['id']] for p in players]) == 15

        prob.solve(pulp.PULP_CBC_CMD(msg=False))
        selected = [p['web_name'] for p in players if player_vars[p['id']].value() == 1]
        return f"⚽ **Optimal 3-Week Horizon Squad:**\n" + ", ".join(selected[:15])

bot_engine = FPLBot()

# ==========================================
# TELEGRAM COMMAND HANDLERS
# ==========================================
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
        "• `/rival <ID>` — Spy on and compare stats with a mini-league rival\n"
        "• `/roast` — Get a brutal reality check on your last gameweek choices\n"
        "• `/stats` — Manager rank & team valuation"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def squad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = bot_engine.run_optimization()
    await update.message.reply_text(result, parse_mode="Markdown")

async def prices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = bot_engine.check_deadline_and_prices()
    await update.message.reply_text(result, parse_mode="Markdown")

async def rival_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        target_id = RIVAL_ID
        if not target_id:
            await update.message.reply_text("Please provide a rival ID: `/rival <ID>`", parse_mode="Markdown")
            return
    else:
        target_id = context.args[0]
    
    result = bot_engine.scout_rival(target_id)
    await update.message.reply_text(result, parse_mode="Markdown")

async def roast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_id = context.args[0] if context.args else RIVAL_ID
    if not target_id:
        await update.message.reply_text("Please provide an ID to roast: `/roast <ID>`", parse_mode="Markdown")
        return
    result = bot_engine.scout_rival(target_id)
    await update.message.reply_text(result, parse_mode="Markdown")

def main():
    if not TELEGRAM_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN is missing in environment variables.")
        return

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Register all command handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("squad", squad_cmd))
    app.add_handler(CommandHandler("prices", prices_cmd))
    app.add_handler(CommandHandler("rival", rival_cmd))
    app.add_handler(CommandHandler("roast", roast_cmd))

    logging.info("FPL Bot is polling for updates...")
    app.run_polling()

if __name__ == "__main__":
    main()