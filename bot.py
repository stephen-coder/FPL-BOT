from datetime import datetime, timezone
import pandas as pd
from pulp import LpMaximize, LpProblem, LpStatus, LpVariable, lpSum
import requests

# ==========================================
# CONFIGURATION & WEBHOOKS
# ==========================================
# Paste your Discord or Telegram Webhook details here (or leave blank to skip)
DISCORD_WEBHOOK_URL = ""  # e.g., "https://discord.com/api/webhooks/..."
TELEGRAM_BOT_TOKEN = ""  # e.g., "123456789:ABCdef..."
TELEGRAM_CHAT_ID = ""  # e.g., "987654321"

FPL_BASE_URL = "https://fantasy.premierleague.com/api/"


# ==========================================
# 1. FETCH FPL DATA
# ==========================================
def fetch_fpl_data():
    """Fetch general FPL data including players, teams, and gameweeks."""
    response = requests.get(f"{FPL_BASE_URL}bootstrap-static/")
    if response.status_code != 200:
        raise Exception("Failed to fetch data from FPL API")
    return response.json()


def get_next_deadline_info(data):
    """Find the next gameweek ID and deadline timestamp."""
    for gw in data["events"]:
        if gw["is_next"]:
            deadline_str = gw["deadline_time"]
            deadline_dt = datetime.fromisoformat(
                deadline_str.replace("Z", "+00:00")
            )
            return gw["id"], deadline_dt
    return None, None


# ==========================================
# 2. SQUAD OPTIMIZER (PuLP Solver)
# ==========================================
def optimize_squad(data, budget=100.0):
    """Solves the 15-player linear optimization problem to maximize expected points (xP)."""
    elements = pd.DataFrame(data["elements"])

    # Calculate xP (using form / ep_next as proxy for expected points)
    elements["xP"] = pd.to_numeric(elements["ep_next"], errors="coerce").fillna(
        0
    )
    elements["now_cost"] = elements["now_cost"] / 10.0  # Convert to millions

    # Filter out unavailable players
    active_players = elements[
        elements["status"].isin(["a", "d"])
    ].reset_index(drop=True)

    # Initialize PuLP Optimization Model
    model = LpProblem(name="FPL-Squad-Optimization", sense=LpMaximize)

    # Binary variable x_i for each player (1 if chosen, 0 if not)
    player_vars = [
        LpVariable(name=f"p_{i}", cat="Binary")
        for i in range(len(active_players))
    ]

    # Objective: Maximize total expected points
    model += (
        lpSum(
            [
                active_players.loc[i, "xP"] * player_vars[i]
                for i in range(len(active_players))
            ]
        ),
        "Total_xP",
    )

    # Constraint 1: Budget limit (£100.0m)
    model += (
        lpSum(
            [
                active_players.loc[i, "now_cost"] * player_vars[i]
                for i in range(len(active_players))
            ]
        )
        <= budget,
        "Budget_Limit",
    )

    # Constraint 2: Total squad size = 15
    model += (
        lpSum([player_vars[i] for i in range(len(active_players))]) == 15,
        "Squad_Size",
    )

    # Constraint 3: Position limits (2 GKP, 5 DEF, 5 MID, 3 FWD)
    positions = {1: 2, 2: 5, 3: 5, 4: 3}
    for pos_id, count in positions.items():
        model += (
            lpSum(
                [
                    player_vars[i]
                    for i in range(len(active_players))
                    if active_players.loc[i, "element_type"] == pos_id
                ]
            )
            == count,
            f"Position_{pos_id}_Limit",
        )

    # Constraint 4: Maximum 3 players per Premier League team
    teams = active_players["team"].unique()
    for team_id in teams:
        model += (
            lpSum(
                [
                    player_vars[i]
                    for i in range(len(active_players))
                    if active_players.loc[i, "team"] == team_id
                ]
            )
            <= 3,
            f"Team_{team_id}_Limit",
        )

    # Solve optimization problem
    model.solve()

    if LpStatus[model.status] == "Optimal":
        selected_indices = [
            i for i in range(len(active_players)) if player_vars[i].varValue == 1
        ]
        squad = active_players.iloc[selected_indices]
        return squad
    else:
        return None


# ==========================================
# 3. NOTIFICATION DISPATCHERS
# ==========================================
def send_notification(message):
    """Sends notification to Discord or Telegram if configured."""
    print(f"\n--- NOTIFICATION OUTPUT ---\n{message}\n---------------------------")

    if DISCORD_WEBHOOK_URL:
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": message})
            print("Successfully sent to Discord!")
        except Exception as e:
            print(f"Failed to send Discord notification: {e}")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message})
            print("Successfully sent to Telegram!")
        except Exception as e:
            print(f"Failed to send Telegram notification: {e}")


# ==========================================
# 4. MAIN SCHEDULED LOGIC
# ==========================================
def format_squad_summary(squad, title):
    pos_map = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
    squad["Pos"] = squad["element_type"].map(pos_map)
    squad = squad.sort_values(by=["element_type", "xP"], ascending=[True, False])

    summary = f"🚨 **FPL ALERT: {title}** 🚨\n\n"
    summary += "**Optimal £100m Squad Pick:**\n"

    total_cost = squad["now_cost"].sum()
    total_xp = squad["xP"].sum()

    for _, player in squad.iterrows():
        summary += f"• [{player['Pos']}] {player['web_name']} - £{player['now_cost']}m (xP: {player['xP']:.1f})\n"

    summary += f"\n💰 **Total Cost:** £{total_cost:.1f}m"
    summary += f"\n📈 **Projected xP:** {total_xp:.1f} points"

    return summary


def main():
    fpl_data = fetch_fpl_data()
    gw_id, deadline = get_next_deadline_info(fpl_data)

    if not deadline:
        print("No upcoming gameweek deadline found.")
        return

    now = datetime.now(timezone.utc)
    hours_left = (deadline - now).total_seconds() / 3600.0

    print(f"Current UTC Time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(
        f"Next Deadline: GW{gw_id} at {deadline.strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )
    print(f"Hours Remaining: {hours_left:.2f} hours")

    # Target trigger windows (checked hourly)
    target_alert = None
    if 47.5 <= hours_left <= 48.5:
        target_alert = f"GW{gw_id} - 48 Hours to Deadline"
    elif 11.5 <= hours_left <= 12.5:
        target_alert = f"GW{gw_id} - 12 Hours to Deadline"
    elif 2.5 <= hours_left <= 3.5:
        target_alert = f"GW{gw_id} - 3 Hours Final Lineup Alert"

    # If triggered by manual test or inside deadline window:
    if target_alert or True:  # Runs directly for testing
        print(f"\nRunning squad optimization algorithm...")
        squad = optimize_squad(fpl_data)

        if squad is not None:
            alert_title = (
                target_alert if target_alert else f"GW{gw_id} Manual Check"
            )
            message = format_squad_summary(squad, alert_title)
            send_notification(message)
        else:
            print("Error: Could not find an optimal squad solution.")


if __name__ == "__main__":
    main()
