# name=bot.py
import os
import logging
import time
import sqlite3
import asyncio
import threading
import requests
import pulp
from flask import Flask, request
from telegram import Update, Bot, BotCommand
from telegram.ext import Application, CommandHandler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # e.g., https://fpl-telegram-bot-63un.onrender.com/webhook

app = Flask(__name__)

# 1. Build the Telegram Application
ptb_application = Application.builder().token(TOKEN).build()

# 2. Create a dedicated background asyncio event loop thread
loop = asyncio.new_event_loop()

def run_async_loop(event_loop):
    asyncio.set_event_loop(event_loop)
    event_loop.run_forever()

threading.Thread(target=run_async_loop, args=(loop,), daemon=True).start()

# 3. Async startup routine to initialize the bot and register the webhook
async def post_init():
    await ptb_application.initialize()
    await ptb_application.start()
    if WEBHOOK_URL:
        await ptb_application.bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"Webhook registered at {WEBHOOK_URL}")

# Dispatch initialization to the background loop immediately on startup
asyncio.run_coroutine_threadsafe(post_init(), loop)

# 4. Flask Webhook Route
@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    if request.headers.get("content-type") == "application/json":
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, ptb_application.bot)
        
        # Safely hand off update processing to the background loop
        asyncio.run_coroutine_threadsafe(
            ptb_application.process_update(update), 
            loop
        )
        return "", 200
    return "Forbidden", 403

@app.route("/", methods=["GET"])
def index():
    return "FPL Telegram Bot is live!", 200
    
# --- Notes ---
# This is the single, consolidated bot: the full command set/ILP engine, running on
# webhooks instead of polling. Key points if you're picking this up cold:
# 1. Telegram updates arrive via POST to /webhook, not via run_polling(). That means
#    an asyncio event loop has to be running continuously in a background thread so
#    Flask's (synchronous) request handler can hand work to it. If that loop is only
#    ever driven once at startup and then left to stop, every later update just times
#    out silently -- this is the bug that made an earlier draft process nothing.
# 2. /live simulates FPL's automatic substitutions and the captain -> vice-captain
#    fallback, so the total matches the official score even when a starter blanks.
# 3. Per-manager endpoints (profile, picks, history, live) are briefly cached
#    (CACHE_TTL_* on FPLBot) so back-to-back commands don't double-hit the FPL API.
# 4. All outbound FPL API calls retry with backoff on timeouts/connection errors/5xx;
#    4xx (e.g. a bad team ID) fails fast.
# 5. SQLite reads/writes run via asyncio.to_thread so one chat's DB call can't stall
#    every other chat's handler.

# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = "https://fpl-telegram-bot-63un.onrender.com/webhook"
# Optional but recommended: set this in your Render env vars. When set, the webhook
# route rejects any POST that doesn't carry Telegram's matching secret header, so a
# stranger who finds your URL can't feed the bot fake updates.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

app = Flask(__name__)

@app.route('/')
def health_check():
    return "FPL Bot is active and healthy!", 200

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

    def _get_team_id_sync(self, chat_id):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT team_id FROM users WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
            return row[0] if row else None

    async def get_team_id(self, chat_id):
        # Off-loaded to a thread so one chat's DB read never blocks the event loop
        # (and therefore every other chat's in-flight command) on sqlite I/O.
        return await asyncio.to_thread(self._get_team_id_sync, chat_id)

    def _set_team_id_sync(self, chat_id, team_id):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO users (chat_id, team_id) VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET team_id = ?
            """, (chat_id, team_id, team_id))
            conn.commit()

    async def set_team_id(self, chat_id, team_id):
        await asyncio.to_thread(self._set_team_id_sync, chat_id, team_id)


class FPLBot:
    CACHE_TTL_STATIC = 300   # bootstrap-static / fixtures
    CACHE_TTL_MANAGER = 60   # manager profile & history -- doesn't move fast
    CACHE_TTL_PICKS = 60     # per-manager GW picks
    CACHE_TTL_LIVE = 20      # live event stats -- short, since scores move during play
    MAX_RETRIES = 3
    RETRY_BACKOFF_BASE = 0.6  # seconds; doubles each retry (0.6, 1.2, ...)

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.db = DatabaseManager()
        self._cache = {}

    # --- Networking: retry/backoff + a single cache path for every FPL endpoint ---
    def _request_with_retry(self, url):
        """Synchronous GET with retry/backoff for transient failures (timeouts,
        connection errors, 5xx). 4xx errors (e.g. an invalid team ID -> 404) fail
        fast since retrying can't change the outcome."""
        last_exc = None
        for attempt in range(self.MAX_RETRIES):
            try:
                res = self.session.get(url, timeout=15)
                if res.status_code >= 500:
                    raise requests.exceptions.HTTPError(f"Server error {res.status_code}", response=res)
                res.raise_for_status()
                return res.json()
            except requests.exceptions.HTTPError as e:
                resp = getattr(e, "response", None)
                if resp is not None and resp.status_code < 500:
                    logger.error(f"Non-retryable HTTP error for {url}: {e}")
                    return None
                last_exc = e
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_exc = e
            except Exception as e:
                logger.error(f"Unexpected error fetching {url}: {e}")
                return None

            if attempt < self.MAX_RETRIES - 1:
                time.sleep(self.RETRY_BACKOFF_BASE * (2 ** attempt))

        logger.error(f"Request failed after {self.MAX_RETRIES} attempts: {url} ({last_exc})")
        return None

    async def _cached_get(self, cache_key, url, ttl):
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached and (now - cached["time"] < ttl):
            return cached["data"]
        data = await asyncio.to_thread(self._request_with_retry, url)
        if data is not None:
            self._cache[cache_key] = {"data": data, "time": now}
        return data

    async def _fetch_bootstrap_static(self):
        return await self._cached_get("bootstrap", f"{FPL_BASE_URL}bootstrap-static/", self.CACHE_TTL_STATIC)

    async def _fetch_fixtures(self):
        return await self._cached_get("fixtures", f"{FPL_BASE_URL}fixtures/", self.CACHE_TTL_STATIC)

    async def _fetch_manager_data(self, team_id):
        return await self._cached_get(f"manager:{team_id}", f"{FPL_BASE_URL}entry/{team_id}/", self.CACHE_TTL_MANAGER)

    async def _fetch_manager_gw_picks(self, team_id, gw):
        return await self._cached_get(
            f"picks:{team_id}:{gw}", f"{FPL_BASE_URL}entry/{team_id}/event/{gw}/picks/", self.CACHE_TTL_PICKS
        )

    async def _fetch_manager_history(self, team_id):
        return await self._cached_get(f"history:{team_id}", f"{FPL_BASE_URL}entry/{team_id}/history/", self.CACHE_TTL_MANAGER)

    async def _fetch_live_event(self, gw):
        return await self._cached_get(f"live:{gw}", f"{FPL_BASE_URL}event/{gw}/live/", self.CACHE_TTL_LIVE)

    @staticmethod
    def _fmt_rank(val):
        return f"{val:,}" if isinstance(val, int) else "—"

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

    # --- Live-gameweek helpers: fixture completion + automatic substitutions ---
    @staticmethod
    def _gw_team_fixtures(fixtures, gw):
        """team_id -> list of this gameweek's fixtures involving that team."""
        m = {}
        for f in fixtures:
            if f['event'] != gw:
                continue
            m.setdefault(f['team_h'], []).append(f)
            m.setdefault(f['team_a'], []).append(f)
        return m

    @staticmethod
    def _team_done(team_fixtures_map, team_id):
        """True once every fixture a team has in this GW is finished — or they have
        none at all (a blank), meaning there's nothing left to wait for."""
        fx = team_fixtures_map.get(team_id)
        if not fx:
            return True
        return all(f.get('finished') or f.get('finished_provisional') for f in fx)

    def _compute_auto_subs(self, picks, players_dict, live_stats, team_fixtures_map):
        """Simulate FPL's automatic substitutions for a live gameweek: a starter whose
        fixture(s) are finished with 0 minutes played gets replaced by the highest-
        priority bench player who did play, without breaking the 1 GK / >=3 DEF /
        >=2 MID / >=1 FWD formation. A starter whose fixture hasn't finished yet is
        left alone — they might still come on. Returns (effective_starters,
        effective_bench, subs), where `subs` is a list of (out_element_id,
        in_element_id) tuples in the order the swaps were made."""

        def minutes_of(eid):
            return live_stats.get(eid, {}).get('minutes', 0) or 0

        def done(eid):
            p = players_dict.get(eid)
            return self._team_done(team_fixtures_map, p['team']) if p else True

        starters = sorted([p for p in picks if p['position'] <= 11], key=lambda x: x['position'])
        bench = sorted([p for p in picks if p['position'] > 11], key=lambda x: x['position'])

        active = {p['element']: p for p in starters}
        bench_pool = list(bench)
        subs = []

        # 1) Goalkeeper — a squad only ever carries one reserve GK, so it's a straight swap
        starting_gk = next((p for p in starters if players_dict.get(p['element'], {}).get('element_type') == 1), None)
        bench_gk = next((p for p in bench_pool if players_dict.get(p['element'], {}).get('element_type') == 1), None)
        if starting_gk and bench_gk:
            if minutes_of(starting_gk['element']) == 0 and done(starting_gk['element']) and minutes_of(bench_gk['element']) > 0:
                del active[starting_gk['element']]
                active[bench_gk['element']] = bench_gk
                bench_pool.remove(bench_gk)
                subs.append((starting_gk['element'], bench_gk['element']))

        # 2) Outfield — walk the bench in the manager's set order, only accepting a
        #    swap that keeps a legal formation.
        def formation_counts(pool):
            c = {2: 0, 3: 0, 4: 0}
            for p in pool.values():
                et = players_dict.get(p['element'], {}).get('element_type')
                if et in c:
                    c[et] += 1
            return c

        gk_out_id = starting_gk['element'] if starting_gk else None
        for starter in starters:
            eid = starter['element']
            if eid == gk_out_id or eid not in active:
                continue
            if minutes_of(eid) > 0 or not done(eid):
                continue  # played, or their fixture(s) aren't finished yet — wait and see

            for cand in list(bench_pool):
                cand_info = players_dict.get(cand['element'])
                if not cand_info or cand_info['element_type'] == 1:
                    continue  # bench GK never covers an outfield slot
                if minutes_of(cand['element']) <= 0:
                    continue  # only bring on a bench player who actually played

                trial = {k: v for k, v in active.items() if k != eid}
                trial[cand['element']] = cand
                c = formation_counts(trial)
                if c[2] >= 3 and c[3] >= 2 and c[4] >= 1:
                    del active[eid]
                    active[cand['element']] = cand
                    bench_pool.remove(cand)
                    subs.append((eid, cand['element']))
                    break

        return list(active.values()), bench_pool, subs

    # --- Telegram Handlers ---
    async def start(self, update: Update, context):
        welcome_text = (
            "⚽ **Welcome to the FPL Assistant Bot!**\n\n"
            "**Setup & Squad:**\n"
            "• `/setteam <ID>` - Link your FPL Team ID to the chat session\n"
            "• `/squad` - Optimal starting XI & lineup for your linked squad\n"
            "• `/stats` - View manager rank, points, and team value\n\n"
            "**Transfers:**\n"
            "• `/transfers` - Multi-week fixture transfer planner\n"
            "• `/hits` - Point hit ROI evaluator\n"
            "• `/scout` - Top projected point-scorers for the upcoming gameweek\n"
            "• `/prices` - Track upcoming deadlines, risers, and fallers\n\n"
            "**Chip Planning:**\n"
            "• `/freehit` - Generate an optimal 15-player Free Hit squad for next GW\n"
            "• `/bestchip` - Scan upcoming DGWs and BGWs for chip timing\n"
            "• `/benchboost` - Simulate optimal 15-player squad for Bench Boost\n"
            "• `/triplecaptain` - Simulate top captaincy options and fixtures\n\n"
            "**Live & Banter:**\n"
            "• `/live` - Real-time live score tracker\n"
            "• `/rival <Team ID>` - Spy on and compare stats with a rival\n"
            "• `/roast` - Deliver a real talk check on gameweek scores"
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
            await self.db.set_team_id(chat_id, team_id)
            name = f"{manager.get('player_first_name', '')} {manager.get('player_last_name', '')}"
            team_name = manager.get('name', 'Unknown Team')
            await update.message.reply_text(f"✅ Successfully linked!\n👤 **Manager:** {name}\n🛡️ **Team:** {team_name}")
        else:
            await update.message.reply_text("❌ Could not verify Team ID from FPL API. Please check and try again.")

    async def squad(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = await self.db.get_team_id(chat_id)
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
        team_id = await self.db.get_team_id(chat_id)
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
        team_id = await self.db.get_team_id(chat_id)
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
        team_id = await self.db.get_team_id(chat_id)
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
        team_id = await self.db.get_team_id(chat_id)
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
        team_id = await self.db.get_team_id(chat_id)
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
        team_id = await self.db.get_team_id(chat_id)
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

    async def scout(self, update: Update, context):
        """Top projected point-scorers for the upcoming GW. Optional args: a max ownership
        %% (e.g. `10`) to scout differentials, and/or a position (GKP/DEF/MID/FWD)."""
        max_own = 100.0
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

        await update.message.reply_text("🔭 Scouting top projected scorers for the upcoming gameweek...")

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
            await update.message.reply_text("❌ No players found matching those filters.")
            return

        candidates.sort(key=lambda x: x['score'], reverse=True)
        header = f"🔭 **Top Scouted Picks (GW {target_gw})**"
        if max_own < 100:
            header += f" — <{max_own:.0f}% owned"
        report = [header + "\n"]
        for p in candidates[:10]:
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m, {p['owned_pct']:.1f}% owned) — xP: {p['score']:.1f}")

        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def stats(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = await self.db.get_team_id(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        await update.message.reply_text("📈 Pulling your manager profile...")

        data = await self._fetch_bootstrap_static()
        if not data:
            await update.message.reply_text("❌ API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)
        manager = await self._fetch_manager_data(team_id)
        if not manager:
            await update.message.reply_text("❌ Couldn't fetch your manager profile.")
            return

        picks_data = await self._fetch_manager_gw_picks(team_id, pick_gw)
        gw_points = picks_data.get('entry_history', {}).get('points') if picks_data else None
        gw_rank = picks_data.get('entry_history', {}).get('rank') if picks_data else None
        bank = (manager.get('last_deadline_bank', 0) or 0) / 10.0
        value = (manager.get('last_deadline_value', 0) or 0) / 10.0

        name = f"{manager.get('player_first_name', '')} {manager.get('player_last_name', '')}".strip()
        team_name = manager.get('name', 'Unknown Team')

        report = [
            f"📈 **{team_name}**",
            f"👤 {name}\n",
            f"🏆 **Overall Rank:** {self._fmt_rank(manager.get('summary_overall_rank'))}",
            f"📊 **Overall Points:** {manager.get('summary_overall_points', '—')}",
            f"📅 **GW{pick_gw} Points:** {gw_points if gw_points is not None else '—'}"
            + (f" (rank {self._fmt_rank(gw_rank)})" if gw_rank else ""),
            f"💰 **Team Value:** £{value:.1f}m",
            f"🏦 **Bank:** £{bank:.1f}m",
        ]
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def best_chip(self, update: Update, context):
        await update.message.reply_text("🔮 Scanning fixture list for double and blank gameweeks...")

        data = await self._fetch_bootstrap_static()
        fixtures = await self._fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)
        all_team_ids = {t['id'] for t in data['teams']}

        LOOKAHEAD = 8
        dgw_report, bgw_report = [], []
        for offset in range(LOOKAHEAD):
            gw = target_gw + offset
            gw_fixtures = [f for f in fixtures if f['event'] == gw]
            if not gw_fixtures:
                continue  # not yet scheduled — don't misreport as a blank

            team_counts = {}
            for f in gw_fixtures:
                team_counts[f['team_h']] = team_counts.get(f['team_h'], 0) + 1
                team_counts[f['team_a']] = team_counts.get(f['team_a'], 0) + 1

            dgw_teams = [t for t, c in team_counts.items() if c >= 2]
            bgw_teams = [t for t in all_team_ids if team_counts.get(t, 0) == 0]
            if dgw_teams:
                dgw_report.append((gw, len(dgw_teams)))
            if bgw_teams:
                bgw_report.append((gw, len(bgw_teams)))

        report = [f"🔮 **Chip Timing Scan (GW {target_gw}-{target_gw + LOOKAHEAD - 1})**\n"]
        if dgw_report:
            report.append("⚡ **Double Gameweeks:**")
            for gw, count in dgw_report:
                report.append(f"• GW{gw}: {count} team(s) with 2 fixtures")
        else:
            report.append("⚡ No confirmed double gameweeks in this window yet.")

        report.append("")
        if bgw_report:
            report.append("🚫 **Blank Gameweeks:**")
            for gw, count in bgw_report:
                report.append(f"• GW{gw}: {count} team(s) with no fixture")
        else:
            report.append("🚫 No confirmed blank gameweeks in this window yet.")

        report.append("")
        if dgw_report:
            best_dgw = max(dgw_report, key=lambda x: x[1])
            report.append(f"💡 **GW{best_dgw[0]}** looks like the strongest week for Bench Boost / Triple Captain ({best_dgw[1]} teams with a double).")
        if bgw_report:
            best_bgw = max(bgw_report, key=lambda x: x[1])
            report.append(f"💡 **GW{best_bgw[0]}** looks like a candidate for Free Hit ({best_bgw[1]} teams blank).")

        report.append("\nℹ️ _Fixture schedules can change (postponements, rearrangements) — always confirm close to the deadline._")
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def live(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = await self.db.get_team_id(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        data = await self._fetch_bootstrap_static()
        fixtures = await self._fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ API unreachable.")
            return

        # Prefer the currently in-progress/most recent GW, not the upcoming one
        live_gw = next((gw['id'] for gw in data['events'] if gw.get('is_current')), None)
        if live_gw is None:
            _, live_gw = self._get_target_and_pick_gw(data)

        picks_data, live_data = await asyncio.gather(
            self._fetch_manager_gw_picks(team_id, live_gw),
            self._fetch_live_event(live_gw),
        )
        if not picks_data or 'picks' not in picks_data or not live_data:
            await update.message.reply_text(f"❌ Couldn't retrieve live data for GW {live_gw}.")
            return

        live_stats = {el['id']: el.get('stats', {}) for el in live_data.get('elements', [])}
        players_dict = {p['id']: p for p in data['elements']}
        team_fixtures_map = self._gw_team_fixtures(fixtures, live_gw)

        # Simulate FPL's automatic substitutions: a starter stuck on 0 minutes once
        # their fixture(s) are finished gets swapped for the best bench player who
        # actually played, same as the official scoring does after the fact.
        effective_starters, _, subs = self._compute_auto_subs(
            picks_data['picks'], players_dict, live_stats, team_fixtures_map
        )
        effective_ids = {p['element'] for p in effective_starters}
        subbed_out_ids = {out_id for out_id, _ in subs}
        subbed_in_ids = {in_id for _, in_id in subs}

        # Captain armband: if the captain got 0 minutes in a now-finished fixture,
        # the multiplier passes to the vice-captain — the same rule FPL applies itself.
        captain_pick = next((p for p in picks_data['picks'] if p.get('is_captain')), None)
        vice_pick = next((p for p in picks_data['picks'] if p.get('is_vice_captain')), None)
        armband_id = captain_pick['element'] if captain_pick else None
        if captain_pick and vice_pick and players_dict.get(captain_pick['element']):
            cap_mins = live_stats.get(captain_pick['element'], {}).get('minutes', 0) or 0
            cap_team = players_dict[captain_pick['element']]['team']
            if cap_mins == 0 and self._team_done(team_fixtures_map, cap_team):
                armband_id = vice_pick['element']

        total_points = 0
        lines = []
        for pick in sorted(picks_data['picks'], key=lambda x: x['position']):
            p_info = players_dict.get(pick['element'])
            if not p_info:
                continue
            pts = live_stats.get(pick['element'], {}).get('total_points', 0)
            in_xi = pick['element'] in effective_ids

            if not in_xi:
                mult = 0
            elif pick['element'] == armband_id:
                # Own multiplier covers the captain (incl. Triple Captain) when they
                # played; a fallback to the vice-captain is always a standard double.
                mult = pick['multiplier'] if pick.get('is_captain') else 2
            else:
                mult = 1
            total_points += pts * mult

            tag = " (C)" if pick.get('is_captain') else (" (VC)" if pick.get('is_vice_captain') else "")
            if pick['element'] in subbed_out_ids:
                status = " [Subbed out]"
            elif pick['element'] in subbed_in_ids:
                status = " [Auto-sub]"
            elif not in_xi:
                status = " [Bench]"
            else:
                status = ""
            mult_tag = f" x{mult}" if mult > 1 else ""
            lines.append(f"• {p_info['web_name']}{tag}{status}: {pts} pts{mult_tag}")

        report = [f"📡 **Live Score — GW{live_gw}**\n", f"🏆 **Total:** {total_points} pts\n", *lines]
        if subs:
            report.append("\n🔁 _Auto-subs applied above for starters who finished on 0 minutes._")
        if vice_pick and armband_id == vice_pick['element']:
            vice_name = players_dict.get(vice_pick['element'], {}).get('web_name', 'your vice-captain')
            report.append(f"👑 _Captain blanked — armband passed to {vice_name}._")
        report.append("\nℹ️ _Refresh with `/live` — this bot doesn't push updates automatically._")
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def prices(self, update: Update, context):
        await update.message.reply_text("💹 Checking deadline and price movement signals...")

        data = await self._fetch_bootstrap_static()
        if not data:
            await update.message.reply_text("❌ API unreachable.")
            return

        next_event = next((e for e in data['events'] if e.get('is_next')), None)
        deadline_line = "⏰ No upcoming deadline found."
        if next_event:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(next_event['deadline_time'].replace('Z', '+00:00'))
                deadline_line = f"⏰ **Next Deadline (GW{next_event['id']}):** {dt.strftime('%a %d %b, %H:%M UTC')}"
            except (ValueError, KeyError):
                deadline_line = f"⏰ **Next Deadline (GW{next_event['id']}):** {next_event.get('deadline_time', '—')}"

        elements = data['elements']
        already_changed = sorted(
            (p for p in elements if p.get('cost_change_event', 0) != 0),
            key=lambda p: abs(p['cost_change_event']), reverse=True
        )
        by_net = sorted(elements, key=lambda p: p.get('transfers_in_event', 0) - p.get('transfers_out_event', 0), reverse=True)
        risers = [p for p in by_net if (p.get('transfers_in_event', 0) - p.get('transfers_out_event', 0)) > 0][:5]
        fallers = list(reversed([p for p in by_net if (p.get('transfers_in_event', 0) - p.get('transfers_out_event', 0)) < 0]))[:5]

        report = [f"💹 **Price Watch**\n", deadline_line, ""]
        if already_changed:
            report.append("📊 **Already changed today:**")
            for p in already_changed[:8]:
                direction = "🔼" if p['cost_change_event'] > 0 else "🔽"
                report.append(f"{direction} {p['web_name']}: £{p['now_cost']/10:.1f}m ({p['cost_change_event']/10:+.1f})")
            report.append("")

        if risers:
            report.append("📈 **Likely risers tonight (net transfers in):**")
            for p in risers:
                net = p.get('transfers_in_event', 0) - p.get('transfers_out_event', 0)
                report.append(f"• {p['web_name']} (£{p['now_cost']/10:.1f}m) — net +{net:,}")
            report.append("")

        if fallers:
            report.append("📉 **Likely fallers tonight (net transfers out):**")
            for p in fallers:
                net = p.get('transfers_in_event', 0) - p.get('transfers_out_event', 0)
                report.append(f"• {p['web_name']} (£{p['now_cost']/10:.1f}m) — net {net:,}")

        report.append("\nℹ️ _Net transfer volume is a signal, not a guarantee — actual changes depend on FPL's internal algorithm._")
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def rival(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = await self.db.get_team_id(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return
        if not context.args or not context.args[0].isdigit():
            await update.message.reply_text("⚠️ Usage: `/rival <Team ID>`", parse_mode="Markdown")
            return
        rival_id = context.args[0]

        await update.message.reply_text("🕵️ Spying on rival squad...")

        data = await self._fetch_bootstrap_static()
        fixtures = await self._fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)

        my_manager, rival_manager = await asyncio.gather(
            self._fetch_manager_data(team_id), self._fetch_manager_data(rival_id)
        )
        if not my_manager or not rival_manager:
            await update.message.reply_text("❌ Couldn't fetch one of the team profiles — check the rival Team ID.")
            return

        my_picks, rival_picks = await asyncio.gather(
            self._fetch_manager_gw_picks(team_id, pick_gw), self._fetch_manager_gw_picks(rival_id, pick_gw)
        )

        players_dict = {p['id']: p for p in data['elements']}

        def projected_total(picks_data):
            if not picks_data or 'picks' not in picks_data:
                return None
            total = 0.0
            for pick in picks_data['picks']:
                if pick['position'] > 11:
                    continue
                p_info = players_dict.get(pick['element'])
                if not p_info:
                    continue
                total += self._calculate_single_gw_xpts(p_info, fixtures, target_gw) * pick['multiplier']
            return total

        my_proj = projected_total(my_picks)
        rival_proj = projected_total(rival_picks)

        def profile_block(label, manager, proj):
            name = f"{manager.get('player_first_name', '')} {manager.get('player_last_name', '')}".strip()
            lines = [
                f"{label} — **{manager.get('name', 'Unknown Team')}** ({name})",
                f"🏆 Rank: {self._fmt_rank(manager.get('summary_overall_rank'))} | 📊 Points: {manager.get('summary_overall_points', '—')}",
                f"💰 Value: £{(manager.get('last_deadline_value', 0) or 0) / 10:.1f}m",
            ]
            if proj is not None:
                lines.append(f"🔮 Projected GW{target_gw}: {proj:.1f} pts")
            return lines

        report = ["🕵️ **Rival Check**\n"]
        report += profile_block("👤 You", my_manager, my_proj)
        report.append("")
        report += profile_block("🎯 Rival", rival_manager, rival_proj)

        if my_proj is not None and rival_proj is not None:
            diff = my_proj - rival_proj
            if diff > 0.5:
                report.append(f"\n✅ You're projected to win this GW by {diff:.1f} pts.")
            elif diff < -0.5:
                report.append(f"\n⚠️ Rival is projected to outscore you by {-diff:.1f} pts.")
            else:
                report.append("\n🤝 Dead even on projection.")

        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def roast(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = await self.db.get_team_id(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        await update.message.reply_text("🔥 Pulling up the tape...")

        data = await self._fetch_bootstrap_static()
        if not data:
            await update.message.reply_text("❌ API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)
        event = next((e for e in data['events'] if e['id'] == pick_gw), None)
        avg_score = event.get('average_entry_score') if event else None
        highest_score = event.get('highest_score') if event else None

        picks_data = await self._fetch_manager_gw_picks(team_id, pick_gw)
        my_score = picks_data.get('entry_history', {}).get('points') if picks_data else None

        if my_score is None or not avg_score:
            await update.message.reply_text("❌ No completed gameweek score to roast yet — check back after kickoff.")
            return

        diff = my_score - avg_score
        report = [f"🔥 **GW{pick_gw} Real Talk**\n", f"Your score: **{my_score}** pts", f"Average: {avg_score} pts"]
        if highest_score:
            report.append(f"Highest: {highest_score} pts")
        report.append("")

        if diff >= 20:
            line = "Absolutely cooked the average. Screenshot this before it regresses to the mean."
        elif diff >= 8:
            line = "Solidly above the pack. Not bragging rights yet, but you're allowed a small smile."
        elif diff > -3:
            line = "Right around the average — the FPL equivalent of a shrug emoji."
        elif diff > -15:
            line = "Below the curve. The captain pick is probably the first suspect."
        else:
            line = "Rough week. This is the kind of scoreline that ends in a Wildcard by Thursday."
        report.append(f"_{line}_")

        await update.message.reply_text("\n".join(report), parse_mode="Markdown")


BOT_COMMANDS = [
    ("start", "Launch the FPL Assistant and view the main menu"),
    ("setteam", "Link your FPL Team ID to the chat session"),
    ("squad", "Optimal starting XI & lineup for your linked squad"),
    ("freehit", "Generate an optimal 15-player Free Hit squad for next GW"),
    ("scout", "Top projected point-scorers for the upcoming gameweek"),
    ("stats", "View manager rank, points, and team value"),
    ("transfers", "Multi-week fixture transfer planner"),
    ("hits", "Point hit ROI evaluator"),
    ("bestchip", "Scan upcoming DGWs and BGWs for chip timing"),
    ("benchboost", "Simulate optimal 15-player squad for Bench Boost"),
    ("triplecaptain", "Simulate top captaincy options and fixtures"),
    ("live", "Real-time live score tracker"),
    ("prices", "Track upcoming deadlines, risers, and fallers"),
    ("rival", "Spy on and compare stats with a rival"),
    ("roast", "Deliver a real talk check on gameweek scores"),
]


# --- Bot / Application setup (webhook mode: no run_polling, no post_init hook) ---
if not TOKEN:
    logger.warning("TELEGRAM_BOT_TOKEN is not set.")

application = Application.builder().token(TOKEN if TOKEN else "000000:INVALID").build()
bot_instance = FPLBot()

application.add_handler(CommandHandler("start", bot_instance.start))
application.add_handler(CommandHandler("setteam", bot_instance.set_team))
application.add_handler(CommandHandler("squad", bot_instance.squad))
application.add_handler(CommandHandler("freehit", bot_instance.free_hit))
application.add_handler(CommandHandler("scout", bot_instance.scout))
application.add_handler(CommandHandler("stats", bot_instance.stats))
application.add_handler(CommandHandler("transfers", bot_instance.transfers))
application.add_handler(CommandHandler("hits", bot_instance.hits))
application.add_handler(CommandHandler("bestchip", bot_instance.best_chip))
application.add_handler(CommandHandler("benchboost", bot_instance.bench_boost))
application.add_handler(CommandHandler("triplecaptain", bot_instance.triple_captain))
application.add_handler(CommandHandler("live", bot_instance.live))
application.add_handler(CommandHandler("prices", bot_instance.prices))
application.add_handler(CommandHandler("rival", bot_instance.rival))
application.add_handler(CommandHandler("roast", bot_instance.roast))
application.add_handler(CommandHandler("wildcard", bot_instance.wildcard))


async def _error_handler(update, context):
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ Something went wrong processing that command. Please try again."
            )
        except Exception:
            pass

application.add_error_handler(_error_handler)


# --- Global event loop running in a background thread ---
_loop = asyncio.new_event_loop()

def _run_loop_forever(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

_loop_thread = threading.Thread(target=_run_loop_forever, args=(_loop,), daemon=True)
_loop_thread.start()


async def _startup():
    await application.initialize()
    await application.start()
    await application.bot.set_my_commands([BotCommand(cmd, desc) for cmd, desc in BOT_COMMANDS])
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not configured -- skipping webhook registration.")
        return
    try:
        await application.bot.set_webhook(
            url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET or None,
        )
        logger.info(f"Webhook registered at {WEBHOOK_URL}")
    except Exception as e:
        logger.error(f"Failed to set webhook (check TELEGRAM_BOT_TOKEN / WEBHOOK_URL): {e}")

try:
    asyncio.run_coroutine_threadsafe(_startup(), _loop).result(timeout=30)
except Exception as e:
    logger.error(f"Bot startup failed: {e}")


# --- Flask Webhook Endpoint ---
@app.route('/webhook', methods=['POST'])
def webhook():
    if WEBHOOK_SECRET:
        secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret_header != WEBHOOK_SECRET:
            return "Unauthorized", 403

    json_data = request.get_json(force=True)
    update = Update.de_json(json_data, application.bot)
    
    if update:
        asyncio.run_coroutine_threadsafe(
            application.process_update(update), 
            _loop
        )
        
    return "OK", 200
