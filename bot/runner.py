from fastapi import FastAPI
from bot.bot import run_bot
import multiprocessing

app = FastAPI()

@app.get("/")
def home(): return {"status": "API Online"}

if __name__ == "__main__":
    # Start Bot in a separate process
    p = multiprocessing.Process(target=run_bot)
    p.start()
    
    # Start API
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)