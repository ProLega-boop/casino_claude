"""main.py — Replit entry point."""
import asyncio, logging
import uvicorn

async def run_server():
    cfg = uvicorn.Config("server:app", host="0.0.0.0", port=8000, log_level="info")
    await uvicorn.Server(cfg).serve()

async def run_bot():
    import bot as B
    await B.dp.start_polling(B.bot)

async def main():
    await asyncio.gather(run_server(), run_bot())

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
