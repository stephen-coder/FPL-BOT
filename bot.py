import datetime
import os
import pandas as pd
import pulp
import requests

# ==========================================
# CONFIGURATION & TELEGRAM CREDENTIALS
# ==========================================
DISCORD_WEBHOOK_URL = ""
TELEGRAM_BOT_TOKEN = "7999571480:AAHLu28JqoZy8vyO90iDoFMtFlhvAYun5Ng"
TELEGRAM_CHAT_ID = "5768618690"

FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"

POSITION_MAP = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


# ==========================================
# NOTIFICATION FUNCTION
# ==========================================
def send_notification(message):
    print(
        f"\n--- NOTIFICATION OUTPUT ---\n{message}\n---------------------------"
    )

    # Send to Telegram
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
            }
            res = requests.post(telegram_url, json=payload)
            if res.status_code == 200:
                print("Successfully sent message to Telegram!")
            else:
                print(
                    f"Telegram error response: {res.status_code} - {res.text}"
                )
        except Exception as e:
            print(f"Failed to send Telegram notification: {e}")

    # Send to Discord
    if DISCORD_WEBHOOK_URL and DISCORD_WEBHOOK_URL.startswith("http"):
        try:
            res = requests.post(
                DISCORD_WEBHOOK_URL, json={"content": message}
            )
            if res.status_code in [200, 204]:
                print("Successfully sent message to Discord!")
            else:
                print(f"Discord error response: {res.status_code} - {res.text}")
        except Exception as e:
            print(f"Failed to send Discord notification: {e}")


# ==========================================
# FPL DATA RETRIEVAL
# ==========================================
def get_fpl_data():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/115.0.0.0 Safari/537.36"
        )
    }
    response = requests.get(FPL_BOOTSTRAP_URL, headers=headers)
    response.raise_for_status()
    return response.json()


# ==========================================
# SQUAD OPTIMIZER (PuLP)
# ==========================================
def optimize_squad(players_df, budget=100.0):
    prob = pulp.LpProblem("FPL_Optimization", pulp.LpMaximize)

    player_vars = pulp.LpVariable.dicts(
        "Player", players_df.index, cat="Binary"
    )

    # Objective: Maximize total predicted points
    prob += (
        pulp.lpSum(
            [
                player_vars[i] * players_df.loc[i, "ep_next"]
                for i in players_df.index
            ]
        ),
        "Total_Expected_Points",
    )

    # Constraint 1: Budget limit (100.0m)
    prob += (
        pulp.lpSum(
            [
                player_vars[i] * players_df.loc[i, "now_cost"]
                for i in players_df.index
            ]
        )
        <= budget,
        "Budget_Limit",
    )

    # Constraint 2: Exactly 15 players
    prob += (
        pulp.lpSum([player_vars[i] for i in players_df.index]) == 15,
        "Total_Players",
    )

    # Constraint 3: Position limits (2 GKP, 5 DEF, 5 MID, 3 FWD)
    prob += (
        pulp.lpSum(
            [
                player_vars[i]
                for i in players_df.index
                if players_df.loc[i, "position"] == "GKP"
            ]
        )
        == 2,
        "GKP_Count",
    )
    prob += (
        pulp.lpSum(
            [
                player_vars[i]
                for i in players_df.index
                if players_df.loc[i, "position"] == "DEF"
            ]
        )
        == 5,
        "DEF_Count",
    )
    prob += (
        pulp.lpSum(
            [
                player_vars[i]
                for i in players_df.index
                if players_df.loc[i, "position"] == "MID"
            ]
        )
        == 5,
        "MID_Count",
    )
    prob += (
        pulp.lpSum(
            [
                player_vars[i]
                for i in players_df.index
                if players_df.loc[i, "position"] == "FWD"
            ]
        )
        == 3,
        "FWD_Count",
    )

    # Constraint 4: Max 3 players per team
    for team_id in players_df["team"].unique():
        prob += (
            pulp.lpSum(
                [
                    player_vars[i]
                    for i in players_df.index
                    if players_df.loc[i, "team"] == team_id
                ]
            )
            <= 3,
            f"Team_Limit_{team_id}",
        )

    prob.solve(pulp.PULP_CBC_CMD(msg=False))

    selected_indices = [
        i for i in players_df.index if pulp.value(player_vars[i]) == 1
    ]
    return players_df.loc[selected_indices]


# ==========================================
# MAIN EXECUTION
# ==========================================
def main():
    print("Fetching FPL data...")
    data = get_fpl_data()

    # Find Next Gameweek Deadline
    events = data["events"]
    next_event = next((e for e in events if e.get("is_next")), None)

    if not next_event:
        print("No upcoming Gameweek deadline found.")
        return

    gw_name = next_event["name"]
    deadline_str = next_event["deadline_time"]
    deadline_dt = datetime.datetime.fromisoformat(
        deadline_str.replace("Z", "+00:00")
    )

    print(f"Upcoming: {gw_name} | Deadline: {deadline_dt}")

    # Build DataFrame
    players = data["elements"]
    df = pd.DataFrame(players)

    df["now_cost"] = df["now_cost"] / 10.0
    df["ep_next"] = pd.to_numeric(df["ep_next"], errors="coerce").fillna(0.0)
    df["position"] = df["element_type"].map(POSITION_MAP)

    # Optimize Squad
    print("Running squad optimization algorithm...")
    optimal_squad = optimize_squad(df)

    # Format Output Message
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

    total_cost = optimal_squad["now_cost"].sum()
    total_xp = optimal_squad["ep_next"].sum()

    msg += f"\n💰 *Total Cost:* £{total_cost:.1f}m\n"
    msg += f"📈 *Projected Squad Points:* {total_xp:.1f}"

    # Send Notification
    send_notification(msg)


if __name__ == "__main__":
    main()
