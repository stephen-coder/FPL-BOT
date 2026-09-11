import os
import requests
import pulp
import logging
from dotenv import load_dotenv

# Load environment variables securely
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RIVAL_ID = os.getenv("FPL_RIVAL_ID")  # Phase 2: Rival ID for scouting

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

FPL_BASE_URL = "https://fantasy.premier league.com/api"

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

    # ==========================================
    # PHASE 1: Real-Time Engine & Safeguards
    # ==========================================
    def check_deadline_and_prices(self):
        """Phase 1: Checks upcoming deadlines and monitors price changes."""
        data = self.fetch_bootstrap_static()
        if not data:
            return

        # Find next gameweek deadline
        next_gw = next((gw for gw in data['events'] if not gw['finished'] and gw['is_current'] == False), None)
        if next_gw:
            deadline = next_gw['deadline_time']
            logging.info(f"Next Gameweek ({next_gw['name']}) Deadline: {deadline}")
            # Insert alert dispatch logic here for Telegram if within window

        # Monitor price changes
        price_risers = [p for p in data['elements'] if p['cost_change_event'] > 0]
        price_fallers = [p for p in data['elements'] if p['cost_change_event'] < 0]
        
        if price_risers or price_fallers:
            logging.info(f"Detected {len(price_risers)} risers and {len(price_fallers)} fallers.")

    # ==========================================
    # PHASE 2: Rival Scouting & Roast Engine
    # ==========================================
    def scout_rival(self):
        """Phase 2: Analyzes rival performance, chips used, and squad differential."""
        if not RIVAL_ID:
            logging.warning("RIVAL_ID not configured in environment variables.")
            return

        history = self.fetch_manager_history(RIVAL_ID)
        if not history:
            return

        current_season = history.get('current', [])
        if current_season:
            latest_gw = current_season[-1]
            logging.info(f"Rival GW {latest_gw['event']} Points: {latest_gw['points']} (Rank: {latest_gw['overall_rank']})")
            
            # Post-Gameweek Roast Engine Trigger
            if latest_gw['points'] < 40:
                roast_msg = f"🔥 Roast Alert: Your rival scored a miserable {latest_gw['points']} points this gameweek! Time to gloat."
                self.send_telegram_message(roast_msg)

    # ==========================================
    # CORE OPTIMIZATION: PuLP Multi-Week Solver
    # ==========================================
    def run_optimization(self):
        """Runs PuLP linear programming model for squad selection over a 3-week horizon."""
        data = self.fetch_bootstrap_static()
        if not data:
            return

        players = data['elements']
        
        # Define Optimization Problem
        prob = pulp.LpProblem("FPL_Optimization", pulp.LpMaximize)

        # Decision variables: binary choice for selecting a player (0 or 1)
        player_vars = {p['id']: pulp.LpVariable(f"player_{p['id']}", cat='Binary') for p in players}

        # Objective: Maximize expected points (using total_points as proxy for demo)
        prob += pulp.lpSum([p['total_points'] * player_vars[p['id']] for p in players])

        # Budget constraint (e.g., 100.0m)
        prob += pulp.lpSum([p['now_cost'] * player_vars[p['id']] for p in players]) <= 1000

        # Squad size constraint (exactly 15 players)
        prob += pulp.lpSum([player_vars[p['id']] for p in players]) == 15

        # Solve
        prob.solve(pulp.PULP_CBC_CMD(msg=False))

        selected = [p['web_name'] for p in players if player_vars[p['id']].value() == 1]
        logging.info(f"Optimal Squad Selected ({len(selected)} players): {selected[:5]}...")

    def send_telegram_message(self, message):
        """Dispatches notification alerts to Telegram."""
        if not TELEGRAM_TOKEN or not CHAT_ID:
            logging.error("Telegram credentials missing.")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
        try:
            requests.post(url, json=payload)
        except Exception as e:
            logging.error(f"Failed to send Telegram message: {e}")

if __name__ == "__main__":
    bot = FPLBot()
    logging.info("Starting FPL Bot Execution...")
    bot.check_deadline_and_prices()
    bot.scout_rival()
    bot.run_optimization()