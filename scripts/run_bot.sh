cat bot-process.txt | xargs kill -TERM
sleep 3
source .venv/bin/activate
nohup uv run uvicorn meno_telegram_bot.main:app > bot.log 2>&1 &
echo $! > bot-process.txt