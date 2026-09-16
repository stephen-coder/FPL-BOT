import os
import logging
import time
import sqlite3
import asyncio
from datetime import datetime
from threading import Thread
import pulp
import requests
from flask import Flask
from telegram import Update, BotCommand
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

    def _get_team_id_sync(self, chat_id):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT team_id FROM users WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
            return row[0] if row else None

    async def get_team_id(self, chat_id):
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
    CACHE_TTL_STATIC = 300   
    CACHE_TTL_MANAGER = 60   
    CACHE_TTL_PICKS = 60     
    CACHE_TTL_LIVE = 20      
    MAX_RETRIES = 3
    RETRY_BACKOFF_BASE = 0.6  

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.db = DatabaseManager()
        self._cache = {}

    def _request_with_retry(self, url):
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
        return not self._is_hard_excluded(p) and not self._is_doubtful(p)

    def _calculate_single_gw_xpts(self, player, fixtures, target_gw, apply_doubt_penalty=True):
        team_id = player['team']
        base_ep = float(player.get('ep_next', 0) or 0)
        form = float(player.get('form', 0) or 0)

        if self._is_hard_excluded(player):
            return -50.0

        gw_fixtures = [f for f in fixtures if f['event'] == target_gw and (f['team_h'] == team_id or f['team_a'] == team_id)]

        if not gw_fixtures:
            return 0.0

        total_gw_score = 0.0
        for f in gw_fixtures:
            is_home = (f['team_h'] == team_id)
            fdr = f['team_h_difficulty'] if is_home else f['team_a_difficulty']
            gw_score = (base_ep * 1.8) + (form * 0.4) - (fdr * 0.7)
            total_gw_score += max(gw_score, 0.5)

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
            gw_score = self._calculate_single_gw_xpts(player, fixtures, gw, apply_doubt_penalty=(offset == 0))
            total_score += (gw_score * weight)

        return total_score

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

    @staticmethod
    def _gw_team_fixtures(fixtures, gw):
        m = {}
        for f in fixtures:
            if f['event'] != gw:
                continue
            m.setdefault(f['team_h'], []).append(f)
            m.setdefault(f['team_a'], []).append(f)
        return m

    @staticmethod
    def _team_done(team_fixtures_map, team_id):
        fx = team_fixtures_map.get(team_id)
        if not fx:
            return True
        return all(f.get('finished') or f.get('finished_provisional') for f in fx)

    def _compute_auto_subs(self, picks, players_dict, live_stats, team_fixtures_map):
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

        starting_gk = next((p for p in starters if players_dict.get(p['element'], {}).get('element_type') == 1), None)
        bench_gk = next((p for p in bench_pool if players_dict.get(p['element'], {}).get('element_type') == 1), None)
        if starting_gk and bench_gk:
            if minutes_of(starting_gk['element']) == 0 and done(starting_gk['element']) and minutes_of(bench_gk['element']) > 0:
                del active[starting_gk['element']]
                active[bench_gk['element']] = bench_gk
                bench_pool.remove(bench_gk)
                subs.append((starting_gk['element'], bench_gk['element']))

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
                continue

            for cand in list(bench_pool):
                cand_info = players_dict.get(cand['element'])
                if not cand_info or cand_info['element_type'] == 1:
                    continue
                if minutes_of(cand['element']) <= 0:
                    continue

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

        picks_data = await self._fetch_manager_gw_picks(team_id, pick_gw)
        if picks_data and 'entry_history' in picks_data:
            squad_value = picks_data['entry_history'].get('value', 1000) / 10.0
            bank = picks_data['entry_history'].get('bank', 0) / 10.0
            dynamic_budget = squad_value + bank
        else:
            dynamic_budget = 100.0

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

        await update.message.reply_text(f"🔄 Analyzing optimal transfer plan (up to {n} transfers)...")

        data = await self._fetch_bootstrap_static()
        fixtures = await self._fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ FPL API unreachable.")
            return

        target_gw, pick_gw = self._get_target_and_pick_gw(data)
        pools = await self._build_transfer_pools(team_id, pick_gw, target_gw, fixtures, data, horizon=3)
        if not pools:
            await update.message.reply_text("❌ Could not retrieve team picks for transfer planning.")
            return

        result = await self._solve_transfers(pools["owned_pool"], pools["candidates_pool"], n, pools["dynamic_budget"])
        if not result or not result["in"]:
            await update.message.reply_text("✅ No recommended transfers found — your current squad looks optimal for this horizon.")
            return

        report = [f"🔄 **Recommended Transfer Plan (GW {target_gw})**\n", "📤 **Players Out:**"]
        for p in result["out"]:
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m)")

        report.append("\n📥 **Players In:**")
        for p in result["in"]:
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['name']} (£{p['cost']}m) — xP: {p['score']:.1f}")

        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def stats(self, update: Update, context):
        chat_id = update.effective_chat.id
        team_id = await self.db.get_team_id(chat_id)
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        manager = await self._fetch_manager_data(team_id)
        if not manager:
            await update.message.reply_text("❌ Could not fetch manager stats.")
            return

        name = f"{manager.get('player_first_name', '')} {manager.get('player_last_name', '')}"
        team_name = manager.get('name', 'Unknown')
        overall_points = manager.get('summary_overall_points', 0)
        overall_rank = manager.get('summary_overall_rank', '—')
        team_value = manager.get('last_deadline_value', 1000) / 10.0

        report = (
            f"📊 **Manager Stats**\n\n"
            f"👤 **Name:** {name}\n"
            f"🛡️ **Team:** {team_name}\n"
            f"🏆 **Overall Points:** {overall_points}\n"
            f"🌍 **Overall Rank:** {self._fmt_rank(overall_rank)}\n"
            f"💰 **Squad Value:** £{team_value:.1f}m"
        )
        await update.message.reply_text(report, parse_mode="Markdown")

    async def hits(self, update: Update, context):
        await update.message.reply_text("💡 Point hits cost 4 points. Ensure your incoming player's projected extra output exceeds 4 points over the next 2-3 gameweeks to make it profitable!")

    async def scout(self, update: Update, context):
        data = await self._fetch_bootstrap_static()
        fixtures = await self._fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ API unreachable.")
            return

        target_gw, _ = self._get_target_and_pick_gw(data)
        scored = []
        for p in data['elements']:
            if self._is_hard_excluded(p):
                continue
            score = self._calculate_single_gw_xpts(p, fixtures, target_gw)
            scored.append((p, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:10]

        report = [f"🔭 **Top Scout Picks (GW {target_gw})**\n"]
        for p, score in top:
            report.append(f"• [{POS_NAME[p['element_type']]}] {p['web_name']} (£{p['now_cost']/10.0}m) — xP: {score:.1f}")

        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def prices(self, update: Update, context):
        await update.message.reply_text("📈 Price changes occur overnight based on net transfers in/out. Check FPLStatistics or FantasyFootballFix for live price change predictions.")

    async def bestchip(self, update: Update, context):
        await update.message.reply_text("🗓️ Use `/bestchip` to scan upcoming Blank and Double Gameweeks for optimal Chip strategy (Wildcard, Free Hit, Bench Boost, Triple Captain).")

    async def benchboost(self, update: Update, context):
        await update.message.reply_text("🚀 Bench Boost simulation evaluates your full 15-player squad's projected points for the upcoming gameweek.")

    async def triplecaptain(self, update: Update, context):
        await update.message.reply_text("👑 Triple Captain analysis looks at upcoming single/double fixtures for elite premium assets.")

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

        target_gw, pick_gw = self._get_target_and_pick_gw(data)
        picks_data = await self._fetch_manager_gw_picks(team_id, pick_gw)
        if not picks_data or 'picks' not in picks_data:
            await update.message.reply_text("❌ Could not retrieve live picks.")
            return

        live_data = await self._fetch_live_event(pick_gw)
        live_stats = {item['id']: item['stats'] for item in live_data.get('elements', [])} if live_data else {}
        players_dict = {p['id']: p for p in data['elements']}
        team_fixtures_map = self._gw_team_fixtures(fixtures, pick_gw)

        starters, bench, subs = self._compute_auto_subs(picks_data['picks'], players_dict, live_stats, team_fixtures_map)

        total_points = 0
        report = [f"🔴 **Live Score Tracker (GW {pick_gw})**\n", "🟢 **Active XI:**"]
        
        captain_id = next((p['element'] for p in picks_data['picks'] if p.get('is_captain')), None)
        multiplier = 2 if picks_data.get('active_chip') != '3xc' else 3

        for p in starters:
            eid = p['element']
            p_info = players_dict.get(eid, {})
            pts = live_stats.get(eid, {}).get('total_points', 0)
            is_cap = (eid == captain_id)
            effective_pts = pts * multiplier if is_cap else pts
            total_points += effective_pts
            cap_tag = " (C)" if is_cap else ""
            report.append(f"• {p_info.get('web_name', 'Unknown')}{cap_tag}: {pts} pts (Total: {effective_pts})")

        if subs:
            report.append("\n🔄 **Automatic Substitutions:**")
            for out_id, in_id in subs:
                out_name = players_dict.get(out_id, {}).get('web_name', 'Unknown')
                in_name = players_dict.get(in_id, {}).get('web_name', 'Unknown')
                report.append(f"• {out_name} ➡️ {in_name}")

        report.append(f"\n🏆 **Estimated Live Points:** {total_points}")
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def rival(self, update: Update, context):
        if not context.args:
            await update.message.reply_text("⚠️ Usage: `/rival <Rival Team ID>`", parse_mode="Markdown")
            return
        rival_id = context.args[0]
        manager = await self._fetch_manager_data(rival_id)
        if not manager:
            await update.message.reply_text("❌ Could not fetch rival team details.")
            return
        name = f"{manager.get('player_first_name', '')} {manager.get('player_last_name', '')}"
        team_name = manager.get('name', 'Unknown')
        pts = manager.get('summary_overall_points', 0)
        rank = manager.get('summary_overall_rank', '—')
        await update.message.reply_text(f"🕵️ **Rival Intel**\n\n👤 **Manager:** {name}\n🛡️ **Team:** {team_name}\n🏆 **Points:** {pts}\n🌍 **Rank:** {self._fmt_rank(rank)}")

    async def roast(self, update: Update, context):
        await update.message.reply_text("🔥 Your gameweek score is looking like it was assembled by a wheelbarrow. Time for a wildcard!")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("No TELEGRAM_BOT_TOKEN environment variable found!")
        return

    bot_app = FPLBot()
    app_tg = Application.builder().token(token).build()

    # Register handlers
    app_tg.add_handler(CommandHandler("start", bot_app.start))
    app_tg.add_handler(CommandHandler("setteam", bot_app.set_team))
    app_tg.add_handler(CommandHandler("squad", bot_app.squad))
    app_tg.add_handler(CommandHandler("freehit", bot_app.free_hit))
    app_tg.add_handler(CommandHandler("transfers", bot_app.transfers))
    app_tg.add_handler(CommandHandler("stats", bot_app.stats))
    app_tg.add_handler(CommandHandler("hits", bot_app.hits))
    app_tg.add_handler(CommandHandler("scout", bot_app.scout))
    app_tg.add_handler(CommandHandler("prices", bot_app.prices))
    app_tg.add_handler(CommandHandler("bestchip", bot_app.bestchip))
    app_tg.add_handler(CommandHandler("benchboost", bot_app.benchboost))
    app_tg.add_handler(CommandHandler("triplecaptain", bot_app.triplecaptain))
    app_tg.add_handler(CommandHandler("live", bot_app.live))
    app_tg.add_handler(CommandHandler("rival", bot_app.rival))
    app_tg.add_handler(CommandHandler("roast", bot_app.roast))

    # Start Flask server thread for Render health checks
    flask_thread = Thread(target=run_web, daemon=True)
    flask_thread.start()

    logger.info("Starting Telegram Bot application...")
    app_tg.run_polling()


if __name__ == "__main__":
    main()
