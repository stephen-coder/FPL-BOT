import os
import logging
import itertools
from datetime import datetime, timezone
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

ALERT_TRACKER = {}

# ==========================================
# 1. CORE UTILITIES & FPL API HELPERS
# ==========================================

def calculate_selling_price(purchase_price: int, current_price: int) -> int:
    """Calculates exact selling price (keeping 50% of profit)."""
    if current_price <= purchase_price:
        return current_price
    profit = current_price - purchase_price
    return purchase_price + (profit // 2)

def calculate_horizon_score(player_id: int, fixtures_data: dict, decay_rate: float = 0.15) -> float:
    """Computes multi-week expected points score with exponential decay."""
    total_score = 0.0
    player_fixtures = fixtures_data.get(player_id, [])
    for t, gw_data in enumerate(player_fixtures[:3]):
        xpts = gw_data.get("expected_points", 0.0)
        weight = 1.0 / ((1.0 + decay_rate) ** t)
        total_score += xpts * weight
    return total_score

def get_next_deadline_info():
    """Fetches the next upcoming gameweek deadline from the official FPL API."""
    try:
        url = "https://fantasy.premierleague.com/api/bootstrap-static/"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            for event in data.get("events", []):
                if event.get("is_next"):
                    deadline_str = event.get("deadline_time")
                    deadline_dt = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
                    return {
                        "id": event.get("id"),
                        "name": event.get("name"),
                        "deadline_time": deadline_dt
                    }
    except Exception as e:
        logger.error(f"Error fetching FPL deadline: {e}")
    return None


# ==========================================
# 2. STRICT MULTI-TRANSFER PACKAGE ENGINE
# ==========================================

def recommend_transfer_package(
    squad_players: list, 
    player_pool: list, 
    bank: int, 
    banked_fts: int, 
    fixtures_data: dict, 
    target_transfers: int = 1
) -> dict:
    """
    Strictly evaluates packages matching the requested target_transfers count.
    """
    target_transfers = max(1, min(4, target_transfers))
    
    # Score squad and sort by weakest horizon scores
    squad_scored = sorted(
        [(calculate_horizon_score(p['id'], fixtures_data), p) for p in squad_players],
        key=lambda x: x[0]
    )
    
    # Ensure we pull enough bottom candidates to form combinations of 'target_transfers'
    candidate_pool_size = max(8, target_transfers + 3)
    weakest_candidates = [p for _, p in squad_scored[:candidate_pool_size]]
    
    best_package = None
    max_net_gain = float('-inf')
    
    extra_transfers = max(0, target_transfers - banked_fts)
    hit_penalty = extra_transfers * 4  # -4 points per hit
    
    # Iterate through exact combinations of N outgoing players
    for out_group in itertools.combinations(weakest_candidates, target_transfers):
        sells = [calculate_selling_price(p['purchase_price'], p['current_price']) for p in out_group]
        combined_cost = sum(sells) + bank
        
        positional_targets = []
        valid_combo = True
        
        for out_p in out_group:
            valid = [
                t for t in player_pool 
                if t['position'] == out_p['position'] 
                and t['id'] not in [p['id'] for p in squad_players]
                and t.get('chance_of_playing', 100) >= 75
            ]
            if not valid:
                valid_combo = False
                break
            positional_targets.append(valid)
            
        if not valid_combo:
            continue
            
        # Test product combinations of incoming targets
        for in_group in itertools.product(*positional_targets):
            # Ensure all incoming players are unique individuals
            if len({t['id'] for t in in_group}) < target_transfers:
                continue
                
            # Check if total cost fits within budget
            if sum(t['cost'] for t in in_group) <= combined_cost:
                out_score = sum(calculate_horizon_score(p['id'], fixtures_data) for p in out_group)
                in_score = sum(calculate_horizon_score(t['id'], fixtures_data) for t in in_group)
                
                gain = in_score - out_score
                net_gain = gain - hit_penalty
                
                if net_gain > max_net_gain:
                    max_net_gain = net_gain
                    best_package = {
                        "package_type": f"{target_transfers}-Transfer Package",
                        "moves": [
                            {
                                "out_name": out_group[i]['name'],
                                "out_pos": out_group[i]['position'],
                                "out_xp": round(calculate_horizon_score(out_group[i]['id'], fixtures_data), 1),
                                "in_name": in_group[i]['name'],
                                "in_cost": in_group[i]['cost'] / 10.0,
                                "in_xp": round(calculate_horizon_score(in_group[i]['id'], fixtures_data), 1)
                            } 
                            for i in range(target_transfers)
                        ],
                        "hit_penalty": hit_penalty,
                        "net_gain": round(net_gain, 2),
                        "bank_left": round((combined_cost - sum(t['cost'] for t in in_group)) / 10.0, 1)
                    }
                    
    return best_package


# ==========================================
# 3. BACKGROUND JOB QUEUE (DEADLINE REMINDERS)
# ==========================================

async def check_deadline_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = job_data.get("chat_id")
    if not chat_id:
        return

    next_gw = get_next_deadline_info()
    if not next_gw:
        return

    gw_id = next_gw["id"]
    gw_name = next_gw["name"]
    deadline = next_gw["deadline_time"]
    now = datetime.now(timezone.utc)
    
    hours_left = (deadline - now).total_seconds() / 3600.0

    if gw_id not in ALERT_TRACKER:
        ALERT_TRACKER[gw_id] = {48: False, 24: False, 12: False, 2: False}

    milestones = [(48, 46), (24, 22), (12, 10), (2, 0.5)]
    
    for target_hr, lower_bound in milestones:
        if lower_bound <= hours_left <= target_hr and not ALERT_TRACKER[gw_id][target_hr]:
            alert_message = (
                f"🚨 **FPL Deadline Alert!**\n\n"
                f"⏰ **{gw_name} deadline is in ~{target_hr} hours!**\n"
                f"Make sure your transfers, captaincy, and chips are locked in."
            )
            try:
                await context.bot.send_message(chat_id=chat_id, text=alert_message, parse_mode="Markdown")
                ALERT_TRACKER[gw_id][target_hr] = True
            except Exception as e:
                logger.error(f"Failed to send deadline reminder: {e}")
            break


# ==========================================
# 4. TELEGRAM COMMAND HANDLERS
# ==========================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    current_jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    if not current_jobs:
        context.job_queue.run_repeating(
            check_deadline_job, 
            interval=1800, 
            first=10, 
            data={"chat_id": chat_id}, 
            name=str(chat_id)
        )

    welcome_text = (
        "👋 **Welcome to your FPL Assistant Bot!**\n\n"
        "✅ **Automated Deadline Reminders Active:** Alerts set for 48h, 24h, 12h, and 2h before deadlines.\n\n"
        "**Commands:**\n"
        "• `/transfers [1-4]` - Get optimized single or multi-transfer packages.\n"
        "*(Example: `/transfers 3` to evaluate a 3-transfer package)*"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def transfers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_count = 1
    if context.args and context.args[0].isdigit():
        target_count = int(context.args[0])
        
    target_count = max(1, min(4, target_count))
    
    await update.message.reply_text(f"🔄 Analyzing market for the best **{target_count}-transfer package**...", parse_mode="Markdown")
    
    # --- MOCK DATA (Replace with your database/API bindings) ---
    banked_fts = 2  
    bank = 15       # £1.5m in the bank (stored in tenths)
    squad_players = [
        {"id": 1, "name": "Saka", "position": "MID", "purchase_price": 95, "current_price": 100},
        {"id": 2, "name": "Haaland", "position": "FWD", "purchase_price": 140, "current_price": 150},
        {"id": 3, "name": "Pickford", "position": "GKP", "purchase_price": 50, "current_price": 50},
        {"id": 4, "name": "Gabriel", "position": "DEF", "purchase_price": 60, "current_price": 62},
        {"id": 5, "name": "Bowen", "position": "MID", "purchase_price": 75, "current_price": 75},
    ]
    player_pool = [
        {"id": 101, "name": "Palmer", "position": "MID", "cost": 105, "chance_of_playing": 100},
        {"id": 102, "name": "Isak", "position": "FWD", "cost": 85, "chance_of_playing": 100},
        {"id": 103, "name": "Raya", "position": "GKP", "cost": 55, "chance_of_playing": 100},
        {"id": 104, "name": "Saliba", "position": "DEF", "cost": 60, "chance_of_playing": 100},
        {"id": 105, "name": "Mbeumo", "position": "MID", "cost": 75, "chance_of_playing": 100},
    ]
    fixtures_data = {
        1: [{"expected_points": 3.2}, {"expected_points": 4.1}, {"expected_points": 2.5}],
        2: [{"expected_points": 2.1}, {"expected_points": 1.8}, {"expected_points": 2.0}],
        3: [{"expected_points": 1.0}, {"expected_points": 0.9}, {"expected_points": 1.2}],
        4: [{"expected_points": 1.5}, {"expected_points": 1.0}, {"expected_points": 1.1}],
        5: [{"expected_points": 1.2}, {"expected_points": 1.1}, {"expected_points": 1.0}],
        101: [{"expected_points": 7.5}, {"expected_points": 6.8}, {"expected_points": 7.1}],
        102: [{"expected_points": 6.0}, {"expected_points": 6.2}, {"expected_points": 6.8}],
        103: [{"expected_points": 4.5}, {"expected_points": 4.2}, {"expected_points": 4.8}],
        104: [{"expected_points": 4.0}, {"expected_points": 4.1}, {"expected_points": 4.3}],
        105: [{"expected_points": 5.5}, {"expected_points": 5.2}, {"expected_points": 5.0}],
    }
    # -------------------------------------------------------------------
    
    result = recommend_transfer_package(
        squad_players, player_pool, bank, banked_fts, fixtures_data, target_transfers=target_count
    )
    
    if not result:
        await update.message.reply_text(f"❌ No viable {target_count}-transfer packages found within your current budget and positional constraints.")
        return
        
    msg = f"🔀 **Recommended {result['package_type']}**\n"
    if result['hit_penalty'] > 0:
        msg += f"⚠️ *Includes a hit penalty of -{result['hit_penalty']} pts*\n\n"
    else:
        msg += f"✅ *Fully covered by Free Transfers*\n\n"
        
    for move in result['moves']:
        msg += f"🔴 Out: `{move['out_name']}` ({move['out_pos']}) — {move['out_xp']} xP\n"
        msg += f"🟢 In: `{move['in_name']}` (£{move['in_cost']}m) — {move['in_xp']} xP\n\n"
        
    msg += f"📈 **Net Projected Horizon Gain:** +{result['net_gain']} pts\n"
    msg += f"💰 **Bank After:** £{result['bank_left']}m"
    
    await update.message.reply_text(msg, parse_mode="Markdown")


# ==========================================
# 5. MAIN ENTRY POINT
# ==========================================

def main():
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
    
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.warning("⚠️ Warning: You are using a placeholder token. Please set your Telegram Bot Token.")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("transfers", transfers_command))

    logger.info("🤖 FPL Bot with multi-transfer package engine is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
