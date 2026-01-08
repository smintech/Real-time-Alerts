from fastapi import FastAPI
from bot.bot import run_bot  # assuming run_bot() is the blocking polling function
import multiprocessing
import uvicorn

app = FastAPI()


@app.get("/")
def home():
    return {"status": "API Online"}


if __name__ == "__main__":
    # Start Telegram bot in a separate process (polling is blocking)
    bot_process = multiprocessing.Process(target=run_bot, daemon=True)
    bot_process.start()

    # Start FastAPI with Uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

    # Optional: clean shutdown (though daemon=True will kill bot on exit)
    bot_process.join()