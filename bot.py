import datetime
import os
import threading
import pandas as pd
import pulp
import requests
import telebot
from flask import Flask

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN", "7999571480:AAHLu28JqoZy8vyO90iDoFMtFlhvAYun5Ng"
)
FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
POSITION_MAP = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# Initialize Bot & Web Server
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
app = Flask(__name__)


# Keep-Alive Web Endpoint for Render
@app.route("/")
def health_check():
    return "FPL Bot is live and running 24/7!", 200


def run_web_server():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


# ==========================================
# SQUAD OPTIMIZATION LOGIC
# ==========================================
def get_fpl_squad_message():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/115.0.0.0 Safari/537.36"
        )
    }
    response = requests.get(FPL_BOOTSTRAP_URL, headers=headers)
    data = response.json()

    events = data["events"]
    next_event = next((e for e in events if e.get("is_next")), None)
    if not next_event:
        return "No upcoming Gameweek deadline found."

    gw_name = next_event["name"]
    deadline_dt = datetime.datetime.fromisoformat(
        next_event["deadline_time"].replace("Z", "+00:00")
    )

    df = pd.DataFrame(data["elements"])
    df["now_cost"] = df["now_cost"] / 10.0
    df["ep_next"] = pd.to_numeric(df["ep_next"], errors="coerce").fillna(0.0)
    df["position"] = df["element_type"].map(POSITION_MAP)

    # PuLP Optimization Model
    prob = pulp.LpProblem("FPL_Optimization", pulp.LpMaximize)
    player_vars = pulp.LpVariable.dicts("Player", df.index, cat="Binary")

    prob += (
        pulp.lpSum([player_vars[i] * df.loc[i, "ep_next"] for i in df.index]),
        "Total_xP",
    )
    prob += (
        pulp.lpSum([player_vars[i] * df.loc[i, "now_cost"] for i in df.index])
        <= 100.0,
        "Budget",
    )
    prob += pulp.lpSum([player_vars[i] for i in df.index]) == 15, "Total_Players"

    prob += (
        pulp.lpSum(
            [
                player_vars[i]
                for i in df.index
                if df.loc[i, "position"] == "GKP"
            ]
        )
        == 2,
        "GKP",
    )
    prob += (
        pulp.lpSum(
            [
                player_vars[i]
                for i in df.index
                if df.loc[i, "position"] == "DEF"
            ]
        )
        == 5,
        "DEF",
    )
    prob += (
        pulp.lpSum(
            [
                player_vars[i]
                for i in df.index
                if df.loc[i, "position"] == "MID"
            ]
        )
        == 5,
        "MID",
    )
    prob += (
        pulp.lpSum(
            [
                player_vars[i]
                for i in df.index
                if df.loc[i, "position"] == "FWD"
            ]
        )
        == 3,
        "FWD",
    )

    for team_id in df["team"].unique():
        prob += (
            pulp.lpSum(
                [
                    player_vars[i]
                    for i in df.index
                    if df.loc[i, "team"] == team_id
                ]
            )
            <= 3,
            f"Team_{team_id}",
        )

    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    optimal_squad = df.loc[
        [i for i in df.index if pulp.value(player_vars[i]) == 1]
    ]

    msg = f"🚨 *FPL DEADLINE ALERT: {gw_name}* 🚨\n"
    msg += f"⏰ *Deadline:* {deadline_dt.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
    msg += "📋 *Optimal 15-Player Squad (£100m Budget):*\n"

    for pos in ["GKP", "DEF", "MID", "FWD"]:
        msg += f"\n*{pos}s:*\n"
        pos_df = optimal_squad[optimal_squad["position"] == pos].sort_values(
            by="ep_next", ascending=False
        )
        for _, row in pos_df.iterrows():
            msg += f"• {row['web_name']} (£{row['now_cost']}m) - xP: {row['ep_next']}\n"

    msg += f"\n💰 *Total Cost:* £{optimal_squad['now_cost'].sum():.1f}m\n"
    msg += f"📈 *Projected Squad Points:* {optimal_squad['ep_next'].sum():.1f}"
    return msg


# ==========================================
# COMMAND HANDLERS
# ==========================================
@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    welcome_text = (
        "⚽ *FPL Assistant Bot Ready!*\n\n"
        "Available Commands:\n"
        "• /squad or /info - Generate optimal 15-player squad (£100m budget)\n"
        "• /captain - Get top 3 captain candidates by Expected Points (xP)"
    )
    bot.reply_to(message, welcome_text, parse_mode="Markdown")


@bot.message_handler(commands=["squad", "info"])
def send_squad(message):
    bot.reply_to(message, "⏳ Calculating optimal squad... Please wait.")
    try:
        squad_msg = get_fpl_squad_message()
        bot.send_message(message.chat.id, squad_msg, parse_mode="Markdown")
    except Exception as e:
        bot.send_message(
            message.chat.id, f"⚠️ Error calculating squad: {str(e)}"
        )


@bot.message_handler(commands=["captain"])
def send_captain(message):
    bot.reply_to(message, "⏳ Fetching captain recommendations...")
    try:
        response = requests.get(FPL_BOOTSTRAP_URL, timeout=10)
        data = response.json()

        teams = {t["id"]: t["short_name"] for t in data.get("teams", [])}
        players = []

        for p in data.get("elements", []):
            if p.get("ep_next") is not None:
                try:
                    players.append(
                        {
                            "name": p["web_name"],
                            "team": teams.get(p["team"], "UNK"),
                            "ep": float(p["ep_next"]),
                        }
                    )
                except ValueError:
                    continue

        top_3 = sorted(players, key=lambda x: x["ep"], reverse=True)[:3]

        if not top_3:
            bot.send_message(
                message.chat.id, "Could not retrieve captain data."
            )
            return

        msg = "👑 *Top 3 Captain Candidates (Expected Points):*\n\n"
        for idx, p in enumerate(top_3, start=1):
            msg += f"{idx}. *{p['name']}* ({p['team']}) — *{p['ep']:.1f} xP*\n"

        bot.send_message(message.chat.id, msg, parse_mode="Markdown")
    except Exception as e:
        bot.send_message(
            message.chat.id, f"⚠️ Error fetching captain data: {str(e)}"
        )


# ==========================================
# MAIN ENTRY POINT
# ==========================================
if __name__ == "__main__":
    # Run Flask in a background thread
    server_thread = threading.Thread(target=run_web_server)
    server_thread.daemon = True
    server_thread.start()

    print("Bot is live and polling for Telegram commands...")
    bot.infinity_polling()
