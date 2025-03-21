import asyncio
import logging
import random
import json
from functools import partial
from pathlib import Path
import contextlib

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command
from config import settings

import aiohttp
from aiohttp import ClientTimeout

logging.basicConfig(level=logging.INFO)
router = Router()
pending_users = set()

# Глобально загружаем фразы из JSON
PHRASES = {
    "thinking": ["Печатаю ответ..."],
    "fallback": ["Не удалось получить ответ."]
}


def load_phrases(path: str = "phrases.json"):
    global PHRASES
    try:
        with open(path, "r", encoding="utf-8") as f:
            PHRASES = json.load(f)
    except Exception as e:
        logging.warning(f"Не удалось загрузить фразы из {path}: {e}")


def random_phrase(category: str) -> str:
    return random.choice(PHRASES.get(category, ["..."]))


async def get_backend_response(payload: dict, session: aiohttp.ClientSession) -> str:
    try:
        async with session.post(settings.backend_api_url, json=payload) as response:
            if response.status == 200:
                data = await response.json()
                return data.get("response", random_phrase("fallback"))
            else:
                return f"Ошибка API: {response.status}"
    except Exception as e:
        logging.exception("Ошибка при запросе к backend:")
        return random_phrase("fallback")


async def start_handler(message: types.Message):
    await message.answer(
        """Привет, меня зовут Менон, я виртуальный помощник Новосибирского Государственного Университета!
        Мои разработчики попросили сообщить вам следующее:
        Данная нейронная сеть предназначена для предоставления информации и ответов на вопросы, касаемых Новосибирского Государственного Университета. 
Однако, она может генерировать ответы, которые могут быть восприняты как оскорбительные, дискриминационные или неподобающие. Пользователь обязан самостоятельно оценивать и фильтровать как вводные, так и полученные данные. 
Команда разработчиков не несет ответственности за любые последствия, возникшие в результате использования данной нейронной сети, включая, но не ограничиваясь, моральный ущерб, дискриминацию или нарушение прав третьих лиц.""")


async def process_backend(message: types.Message, session: aiohttp.ClientSession, msg_to_edit: types.Message, bot: Bot):
    user_id = message.from_user.id
    payload = {"chat_id": message.chat.id, "message": message.text}

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        logging.info(f"Отправка запроса на бэкенд с payload: {payload}")
        reply = await get_backend_response(payload, session)

        await msg_to_edit.edit_text(reply)
    except Exception as e:
        logging.error(f"Ошибка при обработке запроса: {e}")
        await msg_to_edit.edit_text(random_phrase("fallback"))
    finally:
        pending_users.discard(user_id)  # всегда разблокируем


async def message_handler(message: types.Message, session: aiohttp.ClientSession, bot: Bot):
    user_id = message.from_user.id

    if user_id in pending_users:
        await message.answer("⏳ Пожалуйста, дождитесь ответа на предыдущий запрос.")
        return

    pending_users.add(user_id)

    try:
        # показываем заглушку
        thinking_msg = await message.answer(random_phrase("thinking"))

        # запускаем бэкграунд-задачу
        await asyncio.create_task(
            process_backend(message, session, thinking_msg, bot)
        )
    except Exception as e:
        logging.error(f"Ошибка в message_handler: {e}")
        pending_users.discard(user_id)  # если что-то сломалось — разблокируем


async def clear_history_handler(message: types.Message, session: aiohttp.ClientSession):
    reset_url = settings.backend_api_url.replace("/chat", "/clear_history")
    payload = {"chat_id": message.chat.id}

    try:
        async with session.post(reset_url, json=payload) as response:
            if response.status == 200:
                await message.answer("🧹Начнём с чистого листа, я всё забыл! 😶‍🌫️")
            else:
                await message.answer(f"Ой-ой, что-то пошло не так, скоро меня починят😖")
    except Exception as e:
        logging.exception("Ошибка при очистке истории:")
        await message.answer("Ой-ой, что-то пошло не так, скоро меня починят😖")


async def main():
    load_phrases()

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    timeout = ClientTimeout(total=100)
    session = aiohttp.ClientSession(timeout=timeout)

    # Регистрация хендлеров
    router.message.register(start_handler, Command("start"))
    router.message.register(partial(clear_history_handler, session=session), Command("clear_history"))
    router.message.register(partial(message_handler, session=session, bot=bot), F.text)

    dp.include_router(router)

    logging.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
