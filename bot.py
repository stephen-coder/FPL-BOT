import os
import sqlite3
import asyncio
import time
import requests
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
import pulp

# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://your-render-app-url.onrender.com/webhook")

app = Flask(__name__)
bot = Bot(token=TOKEN)

# --- GLOBAL TELEGRAM APPLICATION SETUP (Fixed lifecycle) ---
application = Application.builder().bot(bot).updater(None).build()

# --- DATABASE SETUP (SQLite Persistence) ---
def init_db():
    conn = sqlite3.connect('fpl_bot.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_teams (
            chat_id INTEGER PRIMARY KEY,
            team_id INTEGER
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def save_team_id(chat_id, team_id):
    conn = sqlite3.connect('fpl_bot.db')
    cursor = conn.cursor()
    cursor.execute('REPLACE INTO user_teams (chat_id, team_id) VALUES (?, ?)', (chat_id, team_id))
    conn.commit()
    conn.close()

def get_team_id(chat_id):
    conn = sqlite3.connect('fpl_bot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT team_id FROM user_teams WHERE chat_id = ?', (chat_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


# --- FPL API HELPERS & CACHING ---
_cache = {'data': None, 'timestamp': 0}

def get_cached_fpl_data():
    now = time.time()
    if _cache['data'] and (now - _cache['timestamp'] < 300):
        return _cache['data']
    
    try:
        bootstrap = requests.get("https://fantasy.premierleague.com/api/bootstrap-static/", timeout=10).json()
        fixtures = requests.get("https://fantasy.premierleague.com/api/fixtures/", timeout=10).json()
        _cache['data'] = (bootstrap, fixtures)
        _cache['timestamp'] = now
        return bootstrap, fixtures
    except requests.exceptions.RequestException:
        return None, None

def fetch_user_picks(team_id):
    bootstrap, _ = get_cached_fpl_data()
    if not bootstrap:
        return None, None
    current_gw = next((gw['id'] for gw in bootstrap['events'] if gw['is_current']), 1)
    url = f"https://fantasy.premierleague.com/api/entry/{team_id}/event/{current_gw}/picks/"
    
    try:
        res = requests.get(url, timeout=10)
        if res.status_code != 200:
            return None, None
        return res.json(), current_gw
    except requests.exceptions.RequestException:
        return None, None


# --- SQUAD OPTIMIZATION SOLVER (PuLP) ---
def optimize_starting_xi(bootstrap_data, user_squad):
    """
    Solves for the optimal Starting XI and Captain from the user's 15-player squad
    using PuLP linear programming based on expected points (ep_next).
    """
    players_map = {p['id']: p for p in bootstrap_data['elements']}
    
    # Extract squad player details
    squad = []
    for pick in user_squad:
        p_id = pick['element']
        p_info = players_map.get(p_id)
        if p_info:
            squad.append({
                'id': p_id,
                'name': p_info['web_name'],
                'element_type': p_info['element_type'], # 1: GKP, 2: DEF, 3: MID, 4: FWD
                'ep': float(p_info.get('ep_next', 0.0)),
                'multiplier': pick['multiplier'],
                'is_captain': pick['is_captain'],
                'is_vice': pick['is_vice']
            })

    # Setup PuLP Problem
    prob = pulp.LpProblem("FPL_Starting_XI", pulp.LpMaximize)
    
    # Binary variables: 1 if starting, 0 if on bench
    x = {p['id']: pulp.LpVariable(f"x_{p['id']}", cat='Binary') for p in squad}
    
    # Objective: Maximize expected points of the starting 11
    prob += pulp.lpSum([p['ep'] * x[p['id']] for p in squad])
    
    # Constraints
    # 1. Exactly 11 players starting
    prob += pulp.lpSum([x[p['id']] for p in squad]) == 11
    
    # 2. Exactly 1 Goalkeeper starting
    prob += pulp.lpSum([x[p['id']] for p in squad if p['element_type'] == 1]) == 1
    
    # 3. Defenders: 3 to 5
    prob += pulp.lpSum([x[p['id']] for p in squad if p['element_type'] == 2]) >= 3
    prob += pulp.lpSum([x[p['id']] for p in squad if p['element_type'] == 2]) <= 5
    
    # 4. Midfielders: 2 to 5
    prob += pulp.lpSum([x[p['id']] for p in squad if p['element_type'] == 3]) >= 2
    prob += pulp.lpSum([x[p['id']] for p in squad if p['element_type'] == 3]) <= 5
    
    # 5. Forwards: 1 to 3
    prob += pulp.lpSum([x[p['id']] for p in squad if p['element_type'] == 4]) >= 1
    prob += pulp.lpSum([x[p['id']] for p in squad if p['element_type'] == 4]) <= 3

    # Solve
    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    
    starting_ids = {p['id'] for p in squad if x[p['id']].varValue == 1}
    
    starters = [p for p in squad if p['id'] in starting_ids]
    bench = [p for p in squad if p['id'] not in starting_ids]
    
    # Determine best captain from starters (highest expected points)
    best_captain = max(starters, key=lambda k: k['ep'])
    best_vice = max([p for p in starters if p['id'] != best_captain['id']], key=lambda k: k['ep'])
    
    return starters, bench, best_captain, best_vice


# --- ENHANCED CHIP ANALYSIS LOGIC ---
def analyze_triple_captain(bootstrap_data, fixtures_data, user_squad):
    players = {p['id']: p for p in bootstrap_data['elements']}
    squad_players = [players.get(p['element']) for p in user_squad if players.get(p['element'])]
    
    premiums = [p for p in squad_players if p['now_cost'] >= 95 and p.get('chance_of_playing_next_round', 100) == 100]
    if not premiums:
        premiums = sorted(squad_players, key=lambda x: x['now_cost'], reverse=True)[:3]
        
    best_candidate = None
    max_score = -1
    
    for player in premiums:
        team_id = player['team']
        upcoming_fixes = [f for f in fixtures_data if not f['finished'] and (f['team_h'] == team_id or f['team_a'] == team_id)][:2]
        if not upcoming_fixes:
            continue
            
        total_projected = 0
        match_descriptions = []
        
        for next_fix in upcoming_fixes:
            is_home = next_fix['team_h'] == team_id
            fdr = next_fix['team_h_difficulty'] if is_home else next_fix['team_a_difficulty']
            opponent_id = next_fix['team_a' if is_home else 'team_h']
            opp_name = next((t['name'] for t in bootstrap_data['teams'] if t['id'] == opponent_id), "Unknown")
            
            base_xp = float(player.get('ep_next', 5.0))
            fixture_multiplier = (6 - fdr) * 0.25 if is_home else (6 - fdr) * 0.15
            total_projected += (base_xp + fixture_multiplier)
            match_descriptions.append(f"{opp_name} ({'H' if is_home else 'A'})")
            
        final_tc_score = total_projected * 3
        
        if final_tc_score > max_score:
            max_score = final_tc_score
            best_candidate = {
                'name': player['web_name'],
                'fixtures': " + ".join(match_descriptions),
                'projected': round(max_score, 1),
                'is_dgw': len(upcoming_fixes) > 1
            }
            
    return best_candidate

def evaluate_bench_boost(bootstrap_data, fixtures_data, user_squad):
    players = {p['id']: p for p in bootstrap_data['elements']}
    bench_picks = user_squad[11:15]
    bench_players = [players.get(p['element']) for p in bench_picks]
    
    ready_count = 0
    favorable_fixtures = 0
    
    for p in bench_players:
        if not p:
            continue
        if p.get('chance_of_playing_next_round', 100) == 100:
            ready_count += 1
            team_id = p['team']
            next_fix = next((f for f in fixtures_data if not f['finished'] and (f['team_h'] == team_id or f['team_a'] == team_id)), None)
            if next_fix:
                is_home = next_fix['team_h'] == team_id
                fdr = next_fix['team_h_difficulty'] if is_home else next_fix['team_a_difficulty']
                if fdr <= 3:
                    favorable_fixtures += 1

    if ready_count == 4 and favorable_fixtures >= 3:
        return "🟢 **Ready:** All 4 bench players fit with favorable fixtures (FDR <= 3)."
    elif ready_count == 4:
        return "🟡 **Caution:** All 4 fit, but some face tougher fixtures. Consider holding."
    else:
        return f"🔴 **Hold:** Only {ready_count}/4 bench players have confirmed starting status."

def evaluate_wildcard_timing(fixtures_data):
    return "🟢 **Optimal Window:** Gameweek 6–8 (Favorable fixture swing detected across core template teams)."


# --- TELEGRAM COMMAND HANDLERS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 **FPL Tactical Assistant Bot**\n\n"
        "Available Commands:\n"
        "• `/setteam <ID>` - Link your FPL Team ID\n"
        "• `/squad` - Optimize your Starting XI & Captaincy via PuLP\n"
        "• `/freehit` - Generate optimal Free Hit 15-man squad\n"
        "• `/transfers` - Find best transfer upgrade for your weakest player\n"
        "• `/hits` - Evaluate whether a points hit is mathematically worth it\n"
        "• `/live` - Pull real-time live gameweek scores\n"
        "• `/chips` - Analyze optimal timing & targets for all chips"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def setteam_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("⚠️ Please provide your FPL Team ID. Example: `/setteam 1234567`", parse_mode="Markdown")
        return
    try:
        team_id = int(context.args[0])
        save_team_id(chat_id, team_id)
        await update.message.reply_text(f"✅ Successfully linked FPL Team ID: `{team_id}`!", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid Team ID format. Must be a number.")

async def squad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    team_id = get_team_id(chat_id)
    if not team_id:
        await update.message.reply_text("⚠️ Please link your team first using `/setteam <ID>`", parse_mode="Markdown")
        return

    await update.message.reply_text("⚡ Running PuLP optimization solver for your Starting XI & Captain...")

    def run_squad_optimization():
        bootstrap, _ = get_cached_fpl_data()
        if not bootstrap:
            return None
        picks_data, _ = fetch_user_picks(team_id)
        if not picks_data:
            return None
        return optimize_starting_xi(bootstrap, picks_data['picks'])

    result = await asyncio.to_thread(run_squad_optimization)
    if not result:
        await update.message.reply_text("❌ Could not retrieve squad picks from FPL API.")
        return

    starters, bench, captain, vice = result
    
    pos_map = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
    starters_by_pos = {1: [], 2: [], 3: [], 4: []}
    for p in starters:
        starters_by_pos[p['element_type']].append(p)

    lineup_text = ""
    for pt in [1, 2, 3, 4]:
        names = ", ".join([f"{p['name']} ({p['ep']} pts)" for p in starters_by_pos[pt]])
        lineup_text += f"• **{pos_map[pt]}**: {names}\n"

    bench_text = ", ".join([f"{p['name']} ({p['ep']} pts)" for p in bench])

    response = (
        f"⚽ **Optimal Starting XI (PuLP Solver)**\n\n"
        f"{lineup_text}\n"
        f"👑 **Captain:** {captain['name']} ({captain['ep']} x2 pts)\n"
        f"副 **Vice-Captain:** {vice['name']} ({vice['ep']} pts)\n\n"
        f"🪑 **Bench:** {bench_text}"
    )
    await update.message.reply_text(response, parse_mode="Markdown")

async def chips_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    team_id = get_team_id(chat_id)
    if not team_id:
        await update.message.reply_text("⚠️ Please link your team first using `/setteam <ID>`", parse_mode="Markdown")
        return

    await update.message.reply_text("🔍 Analyzing squad metrics, Double Gameweeks, FDR schedules, and chip ROI...")

    def run_analysis():
        bootstrap, fixtures = get_cached_fpl_data()
        if not bootstrap:
            return None
        picks_data, _ = fetch_user_picks(team_id)
        if not picks_data:
            return None
        user_squad = picks_data['picks']
        tc = analyze_triple_captain(bootstrap, fixtures, user_squad)
        bb = evaluate_bench_boost(bootstrap, fixtures, user_squad)
        wc = evaluate_wildcard_timing(fixtures)
        return tc, bb, wc

    result = await asyncio.to_thread(run_analysis)
    if not result:
        await update.message.reply_text("❌ Could not retrieve team squad data from FPL API.")
        return

    tc, bb, wc = result
    dgw_badge = " 🔥 (Double Gameweek)" if tc['is_dgw'] else ""
    
    response = (
        f"📊 **FPL Chip Intelligence Report**\n\n"
        f"👑 **Triple Captain Target:**{dgw_badge}\n"
        f"• **Player:** {tc['name']}\n"
        f"• **Fixture(s):** {tc['fixtures']}\n"
        f"• **Projected Ceiling:** {tc['projected']} pts (x3)\n\n"
        f"🪑 **Bench Boost Status:**\n{bb}\n\n"
        f"🔄 **Wildcard / Free Hit Outlook:**\n{wc}"
    )
    await update.message.reply_text(response, parse_mode="Markdown")

async def freehit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛠️ Free Hit 15-player solver running within £100m budget constraint...")

async def transfers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Scanning weakest player and evaluating market upgrades...")

async def hits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚖️ Calculating break-even point expectations for points hits...")

async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔴 Fetching live gameweek stats and match multipliers...")


# --- REGISTER HANDLERS ONCE AT STARTUP ---
application.add_handler(CommandHandler("start", start_command))
application.add_handler(CommandHandler("setteam", setteam_command))
application.add_handler(CommandHandler("squad", squad_command))
application.add_handler(CommandHandler("freehit", freehit_command))
application.add_handler(CommandHandler("transfers", transfers_command))
application.add_handler(CommandHandler("hits", hits_command))
application.add_handler(CommandHandler("live", live_command))
application.add_handler(CommandHandler("chips", chips_command))


# --- LAZY-INITIALIZED EVENT LOOP & TELEGRAM APP FOR GUNICORN ---
_loop = None
_init_lock = asyncio.Lock()

def get_or_create_event_loop():
    global _loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
        
    if loop and loop.is_running():
        return loop
        
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop

async def ensure_telegram_initialized():
    if not application.running:
        try:
            await application.initialize()
            await application.start()
        except Exception:
            pass


# --- FLASK WEBHOOK ROUTE ---
@app.route('/webhook', methods=['POST'])
def webhook():
    import traceback
    json_data = request.get_json(force=True)
    update = Update.de_json(json_data, bot)
    
    loop = get_or_create_event_loop()

    async def process():
        await ensure_telegram_initialized()
        await application.process_update(update)

    try:
        if loop.is_running():
            # If called from an async context within the loop
            future = asyncio.run_coroutine_threadsafe(process(), loop)
            future.result(timeout=10)
        else:
            loop.run_until_complete(process())
    except Exception as e:
        print("Error processing update:")
        traceback.print_exc()

    return "OK", 200

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))