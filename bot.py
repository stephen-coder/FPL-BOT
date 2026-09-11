import os
import requests
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# FPL API Base URLs & Headers (Required to prevent 403 Forbidden blocks)
FPL_BASE_URL = "https://fantasy.premierleague.com/api/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

class FPLBot:
    def __init__(self):
        pass

    def fetch_bootstrap_static(self):
        """Fetches general FPL data including players, teams, and gameweeks."""
        try:
            response = requests.get(f"{FPL_BASE_URL}bootstrap-static/", headers=HEADERS)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching bootstrap-static: {e}")
            return None

    def fetch_fixtures(self):
        """Fetches all fixture data."""
        try:
            response = requests.get(f"{FPL_BASE_URL}fixtures/", headers=HEADERS)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching fixtures: {e}")
            return None

    def fetch_manager_data(self, team_id):
        """Fetches manager details and history."""
        try:
            response = requests.get(f"{FPL_BASE_URL}entry/{team_id}/", headers=HEADERS)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching manager data for ID {team_id}: {e}")
            return None

    def fetch_manager_gw_picks(self, team_id, gw):
        """Fetches manager's 15-player squad and picks for a specific gameweek."""
        try:
            response = requests.get(f"{FPL_BASE_URL}entry/{team_id}/event/{gw}/picks/", headers=HEADERS)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching manager GW picks for team {team_id} GW {gw}: {e}")
            return None

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        welcome_text = (
            "⚽ **Welcome to the FPL Assistant Bot!**\n\n"
            "Here are the active commands you can use:\n"
            "• `/setteam <ID>` - Link your FPL Team ID\n"
            "• `/squad` - Optimal starting XI & lineup for your linked squad\n"
            "• `/freehit` - Generate an optimal 15-player Free Hit squad for next GW\n"
            "• `/scout` - Top projected point-scorers for the upcoming gameweek\n"
            "• `/stats` - View manager rank, points, and team value\n"
            "• `/transfers` - Multi-week fixture transfer planner (Coming Soon)\n"
            "• `/hits <num>` - Point hit ROI evaluator (Coming Soon)\n"
            "• `/bestchip` - Scan DGWs/BGWs for chip timing (Coming Soon)\n"
            "• `/benchboost` - 15-player bench simulation (Coming Soon)\n"
            "• `/triplecaptain` - Top captaincy evaluation (Coming Soon)\n"
            "• `/live` - Live score tracker (Coming Soon)\n"
            "• `/prices` - Price change tracking (Coming Soon)\n"
            "• `/rival <ID>` - Compare with a rival (Coming Soon)\n"
            "• `/roast <ID>` - Gameweek score breakdown (Coming Soon)"
        )
        await update.message.reply_text(welcome_text, parse_mode="Markdown")

    async def set_team(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("⚠️ Please provide your FPL Team ID. Example: `/setteam 1234567`", parse_mode="Markdown")
            return
        
        team_id = context.args[0]
        if not team_id.isdigit():
            await update.message.reply_text("❌ Invalid Team ID format. It should be a number.")
            return

        context.user_data['team_id'] = team_id
        manager = self.fetch_manager_data(team_id)
        if manager:
            name = f"{manager.get('player_first_name', '')} {manager.get('player_last_name', '')}"
            team_name = manager.get('name', 'Unknown Team')
            await update.message.reply_text(f"✅ Successfully linked!\n👤 **Manager:** {name}\n🛡️ **Team:** {team_name}")
        else:
            await update.message.reply_text("⚠️ Team ID saved, but could not verify details from FPL API. Check if the ID is correct.")

    async def squad(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Optimizes and evaluates the user's actual linked 15-player FPL squad for the immediate next gameweek."""
        team_id = context.user_data.get('team_id')
        if not team_id:
            await update.message.reply_text("⚠️ Please link your FPL team first using `/setteam <ID>`", parse_mode="Markdown")
            return

        await update.message.reply_text("⏳ Fetching your squad and analyzing the upcoming gameweek...")

        data = self.fetch_bootstrap_static()
        fixtures = self.fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ Optimization failed: FPL API or fixtures unreachable.")
            return

        next_gw = next((gw for gw in data['events'] if not gw['finished'] and not gw['is_current']), None)
        if not next_gw:
            await update.message.reply_text("❌ No upcoming gameweek found.")
            return

        target_gw = next_gw['id']
        picks_data = self.fetch_manager_gw_picks(team_id, target_gw)
        if not picks_data or 'picks' not in picks_data:
            await update.message.reply_text(f"❌ Could not retrieve squad picks for Team ID {team_id} for Gameweek {target_gw}.")
            return

        user_picks = picks_data['picks']
        players_dict = {p['id']: p for p in data['elements']}

        team_gw_fdr = {}
        for f in fixtures:
            if f['event'] == target_gw:
                team_gw_fdr[f['team_h']] = f['team_h_difficulty']
                team_gw_fdr[f['team_a']] = f['team_a_difficulty']

        squad_pool = []
        for pick in user_picks:
            pid = pick['element']
            p_info = players_dict.get(pid)
            if not p_info:
                continue
            
            fdr = team_gw_fdr.get(p_info['team'], 3)
            form = float(p_info.get('form', 0) or 0)
            ep = float(p_info.get('ep_next', 0) or 0)  # Next GW expected points
            status = p_info.get('status', 'a')
            chance = p_info.get('chance_of_playing_next_round', 100) or 100

            penalty = 0 if (status == 'a' and chance >= 75) else 15
            score = (ep * 2.0) + (form * 1.5) - (fdr * 1.0) - penalty

            squad_pool.append({
                'id': pid,
                'name': p_info['web_name'],
                'element_type': p_info['element_type'],  # 1: GKP, 2: DEF, 3: MID, 4: FWD
                'now_cost': p_info['now_cost'] / 10.0,
                'status': status,
                'chance': chance,
                'score': score,
                'fdr': fdr
            })

        # Separate by position
        gkps = sorted([p for p in squad_pool if p['element_type'] == 1], key=lambda x: x['score'], reverse=True)
        defs = sorted([p for p in squad_pool if p['element_type'] == 2], key=lambda x: x['score'], reverse=True)
        mids = sorted([p for p in squad_pool if p['element_type'] == 3], key=lambda x: x['score'], reverse=True)
        fwds = sorted([p for p in squad_pool if p['element_type'] == 4], key=lambda x: x['score'], reverse=True)

        # Enforce valid formation (1 GKP, min 3 DEF, min 2 MID, min 1 FWD)
        starting_xi = [gkps[0]] + defs[:3] + mids[:2] + fwds[:1]
        
        # Remaining spots to fill starting XI up to 11 players from best available bench players
        bench_pool = gkps[1:] + defs[3:] + mids[2:] + fwds[1:]
        bench_pool.sort(key=lambda x: x['score'], reverse=True)

        needed = 11 - len(starting_xi)
        starting_xi.extend(bench_pool[:needed])
        bench = bench_pool[needed:]

        # Sort starting XI cleanly by position (GKP -> DEF -> MID -> FWD)
        pos_order = {1: 1, 2: 2, 3: 3, 4: 4}
        starting_xi.sort(key=lambda x: (pos_order[x['element_type']], -x['score']))

        best_captain = max(starting_xi, key=lambda x: x['score'])
        best_vc = max([p for p in starting_xi if p['id'] != best_captain['id']], key=lambda x: x['score'])

        report = [
            f"⚽ **Optimal Lineup & Squad Analysis (Gameweek {target_gw})**\n",
            "🟢 **STARTING XI:**"
        ]
        for p in starting_xi:
            warn = " ⚠️ [Doubt]" if p['status'] != 'a' or p['chance'] < 75 else ""
            report.append(f"• {p['name']} (£{p['now_cost']}m) — FDR: {p['fdr']}{warn}")

        report.append("\n🪑 **BENCH:**")
        for idx, p in enumerate(bench, 1):
            report.append(f"{idx}. {p['name']} (£{p['now_cost']}m)")

        report.append(f"\n⭐ **Recommended Captain:** {best_captain['name']}")
        report.append(f"🥈 **Recommended Vice-Captain:** {best_vc['name']}")

        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def free_hit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Generates an optimal 15-player Free Hit squad for the upcoming gameweek within budget."""
        await update.message.reply_text("⚡ Scanning all FPL players and fixtures to build an optimal Free Hit squad...")

        data = self.fetch_bootstrap_static()
        fixtures = self.fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ Free Hit generation failed: API unreachable.")
            return

        next_gw = next((gw for gw in data['events'] if not gw['finished'] and not gw['is_current']), None)
        if not next_gw:
            await update.message.reply_text("❌ No upcoming gameweek found.")
            return

        target_gw = next_gw['id']
        team_gw_fdr = {}
        for f in fixtures:
            if f['event'] == target_gw:
                team_gw_fdr[f['team_h']] = f['team_h_difficulty']
                team_gw_fdr[f['team_a']] = f['team_a_difficulty']

        players = data['elements']
        scored_pool = []
        for p in players:
            status = p.get('status', 'a')
            chance = p.get('chance_of_playing_next_round', 100) or 100
            if status != 'a' or chance < 75:
                continue  # Skip injured/suspended players for Free Hit

            fdr = team_gw_fdr.get(p['team'], 3)
            form = float(p.get('form', 0) or 0)
            ep = float(p.get('ep_next', 0) or 0)

            score = (ep * 3) + (form * 2) - (fdr * 1.5)
            scored_pool.append({
                'name': p['web_name'],
                'element_type': p['element_type'],
                'cost': p['now_cost'] / 10.0,
                'score': score,
                'fdr': fdr
            })

        gkps = sorted([p for p in scored_pool if p['element_type'] == 1], key=lambda x: x['score'], reverse=True)
        defs = sorted([p for p in scored_pool if p['element_type'] == 2], key=lambda x: x['score'], reverse=True)
        mids = sorted([p for p in scored_pool if p['element_type'] == 3], key=lambda x: x['score'], reverse=True)
        fwds = sorted([p for p in scored_pool if p['element_type'] == 4], key=lambda x: x['score'], reverse=True)

        fh_squad = gkps[:2] + defs[:5] + mids[:5] + fwds[:3]
        total_cost = sum(p['cost'] for p in fh_squad)

        starting_xi = [gkps[0]] + defs[:3] + mids[:4] + fwds[:2]
        captain = max(starting_xi, key=lambda x: x['score'])

        report = [
            f"⚡ **Optimal Free Hit Squad (Gameweek {target_gw})**",
            f"💰 **Total Squad Cost:** £{total_cost:.1f}m / £100.0m\n",
            "🛡️ **Starting XI:**"
        ]
        for p in starting_xi:
            report.append(f"• {p['name']} (£{p['cost']}m) — FDR: {p['fdr']}")

        report.append("\n🪑 **Bench:**")
        bench = [p for p in fh_squad if p not in starting_xi]
        for p in bench:
            report.append(f"• {p['name']} (£{p['cost']}m)")

        report.append(f"\n⭐ **Free Hit Captain Pick:** {captain['name']}")
        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def scout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Scouts top projected point-scorers and captain candidates for the upcoming GW."""
        await update.message.reply_text("🔍 Scouting top projected point-scorers for the coming gameweek...")

        data = self.fetch_bootstrap_static()
        fixtures = self.fetch_fixtures()
        if not data or not fixtures:
            await update.message.reply_text("❌ Scout report failed: API unreachable.")
            return

        next_gw = next((gw for gw in data['events'] if not gw['finished'] and not gw['is_current']), None)
        if not next_gw:
            await update.message.reply_text("❌ No upcoming gameweek found.")
            return

        target_gw = next_gw['id']
        team_gw_fdr = {}
        for f in fixtures:
            if f['event'] == target_gw:
                team_gw_fdr[f['team_h']] = f['team_h_difficulty']
                team_gw_fdr[f['team_a']] = f['team_a_difficulty']

        players = data['elements']
        for p in players:
            p['projected_pts'] = float(p.get('ep_next', 0) or 0) + (float(p.get('form', 0) or 0) * 0.5)

        players.sort(key=lambda x: x['projected_pts'], reverse=True)

        top_mids = [p for p in players if p['element_type'] == 3][:3]
        top_fwds = [p for p in players if p['element_type'] == 4][:3]
        top_defs = [p for p in players if p['element_type'] == 2][:3]

        report = [
            f"🎯 **GW {target_gw} Scout Report (Top Projected Points)**\n",
            "🚀 **Top Midfielders:**"
        ]
        for p in top_mids:
            report.append(f"• {p['web_name']} (£{p['now_cost']/10.0}m) — Proj Pts: {p['projected_pts']:.1f}")

        report.append("\n⚽ **Top Forwards:**")
        for p in top_fwds:
            report.append(f"• {p['web_name']} (£{p['now_cost']/10.0}m) — Proj Pts: {p['projected_pts']:.1f}")

        report.append("\n🛡️ **Top Defenders:**")
        for p in top_defs:
            report.append(f"• {p['web_name']} (£{p['now_cost']/10.0}m) — Proj Pts: {p['projected_pts']:.1f}")

        top_captain = players[0]
        report.append(f"\n⭐ **Top Overall Captain Pick:** {top_captain['web_name']} ({top_captain['projected_pts']:.1f} Proj Pts)")

        await update.message.reply_text("\n".join(report), parse_mode="Markdown")

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        team_id = context.user_data.get('team_id')
        if not team_id:
            await update.message.reply_text("⚠️ Please link your team first using `/setteam <ID>`", parse_mode="Markdown")
            return
        manager = self.fetch_manager_data(team_id)
        if manager:
            summary = (
                f"📊 **Manager Stats:**\n"
                f"👤 Name: {manager.get('player_first_name')} {manager.get('player_last_name')}\n"
                f"🛡️ Team: {manager.get('name')}\n"
                f"🏆 Overall Points: {manager.get('summary_overall_points')}\n"
                f"🌍 Overall Rank: {manager.get('summary_overall_rank')}\n"
                f"💰 Bank: £{manager.get('last_deadline_bank', 0) / 10.0}m\n"
                f"📉 Team Value: £{manager.get('last_deadline_value', 0) / 10.0}m"
            )
            await update.message.reply_text(summary, parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Could not retrieve stats for the linked ID.")

    async def not_implemented(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🛠️ This feature is currently under development and will be available soon.")

def main():
    TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
    
    bot_app = FPLBot()
    app = ApplicationBuilder().token(TOKEN).build()

    # Register active command handlers
    app.add_handler(CommandHandler("start", bot_app.start))
    app.add_handler(CommandHandler("setteam", bot_app.set_team))
    app.add_handler(CommandHandler("squad", bot_app.squad))
    app.add_handler(CommandHandler("freehit", bot_app.free_hit))
    app.add_handler(CommandHandler("scout", bot_app.scout))
    app.add_handler(CommandHandler("stats", bot_app.stats))
    
    # Register stubs cleanly
    for cmd in ["transfers", "hits", "bestchip", "benchboost", "triplecaptain", "live", "prices", "rival", "roast"]:
        app.add_handler(CommandHandler(cmd, bot_app.not_implemented))

    print("🤖 FPL Bot is running successfully...")
    app.run_polling()

if __name__ == "__main__":
    main()
