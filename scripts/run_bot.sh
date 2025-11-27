#!/usr/bin/env bash

cd "$(dirname "$0")/.."

if [ -f bot.pid ]; then
  kill "$(cat bot.pid)" 2>/dev/null || true
  sleep 2
fi

source .venv/bin/activate

export PYTHONPATH="$PWD/src"

nohup python -m meno_telegram_bot.main > bot.log 2>&1 &

echo $! > bot.pid

echo "Bot started, PID=$(cat bot.pid)"