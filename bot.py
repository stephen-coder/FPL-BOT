import os
import logging
import time
import sqlite3
import asyncio
from datetime import datetime
import pulp
import requests
from flask import Flask, request
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Flask Server Setup for Render ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "FPL Bot is active and healthy!", 200
# ------------------------------------

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

    # --- Telegram Handlers ---
    async def start(self, update: Update, context):
        welcome_text = (
            "⚽ **Welcome to the FPL Assistant Bot!**\n\n"
            "• `/setteam <ID>` - Link your FPL Team ID\n"
            "• `/squad` - View your optimal starting XI\n"
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
            await update.message.reply_text("⚠️ Please link your team first using `/setteam <ID>`", parse_mode="Markdown")
            return
        await update.message.reply_text(f"🔍 Fetching optimal squad for team {team_id}...")


# --- Initialize Telegram Application & Webhook Route ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
fpl_bot = FPLBot()

application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", fpl_bot.start))
application.add_handler(CommandHandler("setteam", fpl_bot.set_team))
application.add_handler(CommandHandler("squad", fpl_bot.squad))

# Track initialization state to avoid running it multiple times
_is_initialized = False

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """Endpoint that receives updates from Telegram securely via Webhook"""
    global _is_initialized
    
    # Lazily initialize the Telegram application on the first incoming request
    if not _is_initialized:
        async def init_app():
            await application.initialize()
        
        try:
            asyncio.run(init_app())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(init_app())
        
        _is_initialized = True

    if request.method == "POST":
        json_update = request.get_json(force=True)
        update = Update.de_json(json_update, application.bot)
        
        # Safely process the update asynchronously
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(application.process_update(update), loop)
            else:
                asyncio.run(application.process_update(update))
        except Exception:
            asyncio.run(application.process_update(update))
            
    return "OK", 200