from bot import app, start_telegram_bot
import threading

# Start the Telegram bot in a background thread when Gunicorn boots the app
bot_thread = threading.Thread(target=start_telegram_bot)
bot_thread.daemon = True
bot_thread.start()

if __name__ == "__main__":
    app.run()
