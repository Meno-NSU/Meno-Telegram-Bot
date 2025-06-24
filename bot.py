import asyncio
import contextlib
import json
import logging
import random
import re
import time
from collections import defaultdict
from functools import partial

import aiohttp
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command
from aiogram.types import BotCommand
from aiohttp import ClientTimeout

from config import settings

logging.basicConfig(level=logging.INFO)
ESCAPE_CHARS_RE = re.compile(r"([\[\]()~>+\-=|{}.!])")
router = Router()
pending_users = set()
last_typing_times = defaultdict(lambda: 0)
TYPING_INTERVAL = 4

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
        """Привет, меня зовут Менон! Я виртуальный помощник Новосибирского Государственного Университета!
Мои разработчики попросили сообщить вам следующее, прежде чем вы начнёте мной пользоваться:
        
Данная нейронная сеть предназначена для предоставления информации и ответов на вопросы, касаемых Новосибирского Государственного Университета. 
Однако, она может генерировать ответы, которые могут быть восприняты как оскорбительные, дискриминационные или неподобающие. Пользователь обязан самостоятельно оценивать и фильтровать как вводные, так и полученные данные. 
Команда разработчиков не несет ответственности за любые последствия, возникшие в результате использования данной нейронной сети, включая, но не ограничиваясь, моральный ущерб, дискриминацию или нарушение прав третьих лиц.""")


async def process_backend(message: types.Message, session: aiohttp.ClientSession, msg_to_edit: types.Message, bot: Bot):
    user_id = message.from_user.id
    payload = {"chat_id": str(message.chat.id), "message": message.text}

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        logging.info(f"Отправка запроса на бэкенд с payload: {payload}")
        reply = await get_backend_response(payload, session)
        logging.warning(f"Ответ бэкенда: {repr(reply)}")
        try:
            await msg_to_edit.edit_text(prepare_for_markdown_v2(reply), parse_mode="MarkdownV2")
            # await msg_to_edit.edit_text(reply, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Ошибка форматирования MarkdownV2: {e}")
            await msg_to_edit.edit_text(reply)
    except Exception as e:
        logging.error(f"Ошибка при обработке запроса: {e}")
        await msg_to_edit.edit_text(random_phrase("fallback"))
    finally:
        pending_users.discard(user_id)


async def keep_typing(bot: Bot, chat_id: int):
    try:
        while True:
            now = time.time()
            if now - last_typing_times[chat_id] >= TYPING_INTERVAL:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
                last_typing_times[chat_id] = now
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        pass


async def message_handler(message: types.Message, session: aiohttp.ClientSession, bot: Bot):
    user_id = message.from_user.id

    if user_id in pending_users:
        await message.answer("⏳ Пожалуйста, дождитесь ответа на предыдущий запрос.")
        return

    pending_users.add(user_id)

    thinking_msg = await message.answer(random_phrase("thinking"))

    typing_task = asyncio.create_task(keep_typing(bot, message.chat.id))
    backend_task = asyncio.create_task(process_backend(message, session, thinking_msg, bot))
    try:
        await backend_task
    except Exception as e:
        logging.error(f"Ошибка в message_handler: {e}")
    finally:
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task
        pending_users.discard(user_id)


async def clear_history_handler(message: types.Message, session: aiohttp.ClientSession):
    reset_url = settings.backend_api_url.replace("/chat", "/clear_history")
    payload = {"chat_id": str(message.chat.id)}

    try:
        async with session.post(reset_url, json=payload) as response:
            if response.status == 200:
                await message.answer("🧹Начнём с чистого листа, я всё забыл! 😶‍🌫️")
            else:
                await message.answer(f"Ой-ой, что-то пошло не так, скоро меня починят😖")
    except Exception as e:
        logging.exception("Ошибка при очистке истории:")
        await message.answer("Ой-ой, что-то пошло не так, скоро меня починят😖")


async def info_handler(message: types.Message):
    await message.answer(
        "Меня зовут Менон, я чат-бот Новосибирского Государственного Университета. "
        "Моя задача — помогать вам получать ответы на вопросы, связанные с НГУ, "
        "образовательными программами, поступлением и жизнью в Академгородке.\n\n"
        "Я работаю на основе большой языковой модели с поддержкой поиска по базе знаний университета 📚.\n\n"
        "Иногда я могу генерировать ответы, которые могут быть восприняты как оскорбительные, дискриминационные или неподобающие. Пользователь обязан самостоятельно оценивать и фильтровать как вводные, так и полученные данные. "
        "Команда разработчиков не несёт ответственности за любые последствия, возникшие в результате использования данной нейронной сети, включая, но не ограничиваясь, моральный ущерб, дискриминацию или нарушение прав третьих лиц."
    )


def escape_markdown_v2(text: str) -> str:
    """
    Экранирует спецсимволы MarkdownV2 согласно Telegram Bot API:
    https://core.telegram.org/bots/api#markdownv2-style
    """
    # escape_chars = r"_*[]()~`>#+-=|{}.!\\"
    # return re.sub(f"([{ESCAPE_CHARS_RE.escape(escape_chars)}])", r"\\\1", text)
    return ESCAPE_CHARS_RE.sub(r"\\\1", text)


def convert_double_to_single_stars(text: str) -> str:
    # "**текст**" → "*текст*"
    new = text.replace("**", "*")
    return new


def prepare_for_markdown_v2(text: str) -> str:
    return escape_markdown_v2(convert_double_to_single_stars(text))


@router.message(F.sticker)
async def handle_sticker(message: types.Message):
    await message.answer("🧸 Стикеры — это весело, но я умею только читать текст. Спросите меня что-нибудь текстом!")


@router.message(F.photo)
async def handle_photo(message: types.Message):
    await message.answer(
        "📸 Картинки — это замечательно! Но я пока не понимаю изображения. Попробуйте написать мне вопрос текстом!")


@router.message(F.video)
async def handle_video(message: types.Message):
    await message.answer("🎬 Видео — это здорово, но я разбираюсь только в тексте. Спросите меня что-нибудь!")


@router.message(F.voice)
async def handle_voice(message: types.Message):
    await message.answer("🎤 Голос услышал, но мне бы текст — так я точно пойму и отвечу!")


@router.message(F.video_note)
async def handle_video_note(message: types.Message):
    await message.answer("🎥 Кружочки прикольные, но я пока не умею их понимать. Попробуйте текстом, так веселее!")


@router.message(F.audio)
async def handle_audio(message: types.Message):
    await message.answer("🎧 Музыку люблю, но я бот-помощник, так что давайте пообщаемся текстом!")


@router.message(F.document)
async def handle_document(message: types.Message):
    await message.answer(
        "📄 Файлы — это важно, но пока что я умею работать только с текстом. Спросите меня что-нибудь интересное!")


@router.message(F.animation)
async def handle_animation(message: types.Message):
    await message.answer("🎞️ Гифка засчитана! Но текст — моё всё. Жду вопросик в виде слов!")


@router.message(F.contact)
async def handle_contact(message: types.Message):
    await message.answer("📇 Контакт получил, но я предпочитаю текстовые беседы!")


@router.message(F.location)
async def handle_location(message: types.Message):
    await message.answer("📍 Место зафиксировал! А я вот в НГУ нахожусь, можете меня что-нибудь спросить текстом!")


@router.message(~F.text)
async def handle_unknown(message: types.Message):
    await message.answer(
        "🤷 К сожалению, я пока умею понимать только текст. Напишите мне словами, и я постараюсь помочь!")


async def main():
    load_phrases()

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    timeout = ClientTimeout(total=100)
    session = aiohttp.ClientSession(timeout=timeout)

    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск бота"),
        BotCommand(command="clear_history", description="Очистить историю диалога"),
        BotCommand(command="info", description="Информация о боте"),
        # BotCommand(command="about_us", description="Информация о разработчиках"),
    ])

    # Регистрация хендлеров
    router.message.register(start_handler, Command("start"))
    router.message.register(partial(clear_history_handler, session=session), Command("clear_history"))
    router.message.register(partial(info_handler), Command("info"))
    router.message.register(partial(message_handler, session=session, bot=bot), F.text)

    router.message.register(handle_sticker, F.sticker)
    router.message.register(handle_photo, F.photo)
    router.message.register(handle_video, F.video)
    router.message.register(handle_voice, F.voice)
    router.message.register(handle_video_note, F.video_note)
    router.message.register(handle_audio, F.audio)
    router.message.register(handle_animation, F.animation)
    router.message.register(handle_contact, F.contact)
    router.message.register(handle_location, F.location)
    router.message.register(handle_document, F.document)

    # Fallback: всё остальное, что не текст
    router.message.register(handle_unknown, ~F.text)
    dp.include_router(router)

    logging.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
