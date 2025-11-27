import asyncio
import contextlib
import json
import logging
import random
import re
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from functools import partial

import aiohttp
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command
from aiogram.types import BotCommand
from aiohttp import ClientTimeout

from src.meno_telegram_bot.settings import settings

logging.basicConfig(level=logging.INFO)
router = Router()
pending_users = set()

last_typing_times = defaultdict(lambda: 0)
TYPING_INTERVAL = 4

last_edit_times = defaultdict(lambda: 0.0)
MIN_EDIT_INTERVAL = 0.8

dialog_histories = defaultdict(list)
MAX_HISTORY_MESSAGES = 12

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


def escape_markdown_v2(text: str) -> str:
    """
    Экранирует спецсимволы MarkdownV2 согласно Telegram Bot API:
    https://core.telegram.org/bots/api#markdownv2-style
    """
    escape_chars = r"_*[]()~`>#+-=|{}.!\\"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)


def convert_double_to_single_stars(text: str) -> str:
    # "**текст**" → "*текст*"
    return re.sub(r"\*\*(.*?)\*\*", r"*\1*", text)


def prepare_for_markdown_v2(text: str) -> str:
    return escape_markdown_v2(convert_double_to_single_stars(text))


async def get_backend_response(payload: dict, session: aiohttp.ClientSession) -> str:
    """
    Нестриминговый запрос — OpenAI-совместимый /v1/chat/completions.
    """
    payload = {**payload, "stream": False}

    try:
        async with session.post(settings.backend_api_url, json=payload) as response:
            if response.status != 200:
                return f"Ошибка API: {response.status}"

            data = await response.json()
            try:
                choices = data.get("choices") or []
                if not choices:
                    return random_phrase("fallback")
                msg = choices[0].get("message") or {}
                content = msg.get("content")
                if not content:
                    return random_phrase("fallback")
                return content
            except Exception as e:
                logging.error(f"Ошибка разбора OpenAI-ответа: {e}")
                return random_phrase("fallback")
    except Exception:
        logging.exception("Ошибка при запросе к backend (non-stream):")
        return random_phrase("fallback")


async def stream_backend_response(
        payload: dict,
        session: aiohttp.ClientSession,
) -> AsyncIterator[str]:
    """
    Стриминговый запрос к backend.

    Предполагается, что backend:
    - по POST settings.backend_api_url с params={"stream": "true"}
    - возвращает HTTP-стрим (chunked) с plain text (без JSON),
      каждый chunk — продолжение ответа.

    Если backend отдаёт JSON-чанки — лучше преобразовать их на backend-е
    в чистый текст и уже его стримить.
    """
    payload = {**payload, "stream": True}
    try:
        async with session.post(
                settings.backend_api_url,
                json=payload,
                params={"stream": "true"},
                timeout=None,
        ) as response:
            if response.status != 200:
                logging.error(f"Stream backend error status: {response.status}")
                return
            buffer = ""

            async for chunk in response.content.iter_any():
                if not chunk:
                    continue
                try:
                    buffer += chunk.decode("utf-8", errors="ignore")
                except Exception as e:
                    logging.warning(f"Ошибка декодирования чанка: {e}")
                    continue

                while "\n\n" in buffer:
                    event, buffer = buffer.split("\n\n", 1)
                    lines = event.splitlines()

                    for line in lines:
                        if not line.startswith("data:"):
                            continue
                        data_str = line[len("data:"):].strip()

                        if not data_str:
                            continue

                        if data_str == "[DONE]":
                            return

                        # пробуем распарсить JSON чанка
                        try:
                            obj = json.loads(data_str)
                        except json.JSONDecodeError:
                            logging.warning(f"Не удалось распарсить JSON из SSE: {data_str!r}")
                            continue

                        # формат как у OpenAI: choices[0].delta.content
                        try:
                            choices = obj.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta") or {}
                            piece = delta.get("content")
                            if piece:
                                yield piece
                        except Exception as e:
                            logging.warning(f"Проблема при разборе SSE чанка: {e}")
                            continue

    except Exception:
        logging.exception("Ошибка при стриминговом запросе к backend:")
        return


async def start_handler(message: types.Message):
    await message.answer(
        """Привет, меня зовут Менон! Я виртуальный помощник Новосибирского Государственного Университета!
Мои разработчики попросили сообщить вам следующее, прежде чем вы начнёте мной пользоваться:

Данная нейронная сеть предназначена для предоставления информации и ответов на вопросы, касаемых Новосибирского Государственного Университета. 
Однако, она может генерировать ответы, которые могут быть восприняты как оскорбительные, дискриминационные или неподобающие. Пользователь обязан самостоятельно оценивать и фильтровать как вводные, так и полученные данные. 
Команда разработчиков не несет ответственности за любые последствия, возникшие в результате использования данной нейронной сети, включая, но не ограничиваясь, моральный ущерб, дискриминацию или нарушение прав третьих лиц."""
    )


async def process_backend(
        message: types.Message,
        session: aiohttp.ClientSession,
        msg_to_edit: types.Message,
        bot: Bot,
):
    user_id = message.from_user.id
    chat_id = message.chat.id

    history = dialog_histories[chat_id]
    history.append({"role": "user", "content": message.text})
    messages = history[-MAX_HISTORY_MESSAGES:]

    payload = {
        "model": "menon-1",
        "messages": messages,
        "stream": True,
        "user": str(chat_id),
    }

    raw_answer = ""
    final_answer = None

    try:
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        logging.info(f"Отправка стримингового запроса на backend с payload: {payload}")

        async for piece in stream_backend_response(payload, session):
            if not piece:
                continue

            raw_answer += piece

            now = time.time()
            last_edit = last_edit_times[chat_id]

            if now - last_edit >= MIN_EDIT_INTERVAL:
                last_edit_times[chat_id] = now
                try:
                    prepared = prepare_for_markdown_v2(raw_answer)
                    await msg_to_edit.edit_text(prepared, parse_mode="MarkdownV2")
                except Exception as e:
                    logging.error(f"Ошибка форматирования / edit_text в стриме: {e}")
                    try:
                        await msg_to_edit.edit_text(raw_answer)
                    except Exception as e2:
                        logging.error(f"Не удалось обновить сообщение без Markdown: {e2}")

        if not raw_answer.strip():
            logging.info("Стриминговый ответ пустой, делаем non-stream запрос")
            reply = await get_backend_response(payload, session)
            logging.warning(f"Non-stream ответ backend: {repr(reply)}")
            final_answer = reply
            try:
                prepared = prepare_for_markdown_v2(reply)
                await msg_to_edit.edit_text(prepared, parse_mode="MarkdownV2")
            except Exception as e:
                logging.error(f"Ошибка форматирования MarkdownV2 (fallback): {e}")
                await msg_to_edit.edit_text(reply)
        else:
            final_answer = raw_answer
            try:
                prepared = prepare_for_markdown_v2(raw_answer)
                await msg_to_edit.edit_text(prepared, parse_mode="MarkdownV2")
            except Exception as e:
                logging.error(f"Ошибка финального форматирования MarkdownV2: {e}")
                await msg_to_edit.edit_text(raw_answer)

    except Exception as e:
        logging.error(f"Ошибка при обработке запроса: {e}")
        try:
            fallback = random_phrase("fallback")
            final_answer = final_answer or fallback
            await msg_to_edit.edit_text(fallback)
        except Exception:
            logging.exception("Не удалось отправить fallback-сообщение")
    finally:
        pending_users.discard(user_id)

        if final_answer:
            history.append({"role": "assistant", "content": final_answer})
            if len(history) > 2 * MAX_HISTORY_MESSAGES:
                dialog_histories[chat_id] = history[-2 * MAX_HISTORY_MESSAGES:]


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


async def message_handler(
        message: types.Message,
        session: aiohttp.ClientSession,
        bot: Bot,
):
    user_id = message.from_user.id
    chat_id = message.chat.id

    if user_id in pending_users:
        await message.answer("⏳ Пожалуйста, дождитесь ответа на предыдущий запрос.")
        return

    pending_users.add(user_id)

    thinking_msg = await message.answer(random_phrase("thinking"))

    typing_task = asyncio.create_task(keep_typing(bot, chat_id))
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
    chat_id = message.chat.id
    dialog_histories.pop(chat_id, None)

    reset_url = f"{settings.backend_base_url}/clear_history"
    payload = {"chat_id": str(chat_id)}

    try:
        async with session.post(reset_url, json=payload) as response:
            if response.status == 200:
                await message.answer("🧹Начнём с чистого листа, я всё забыл! 😶‍🌫️")
            else:
                await message.answer("Ой-ой, что-то пошло не так, скоро меня починят😖")
    except Exception:
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


@router.message(F.sticker)
async def handle_sticker(message: types.Message):
    await message.answer("🧸 Стикеры — это весело, но я умею только читать текст. Спросите меня что-нибудь текстом!")


@router.message(F.photo)
async def handle_photo(message: types.Message):
    await message.answer(
        "📸 Картинки — это замечательно! Но я пока не понимаю изображения. Попробуйте написать мне вопрос текстом!"
    )


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
        "📄 Файлы — это важно, но пока что я умею работать только с текстом. Спросите меня что-нибудь интересное!"
    )


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
        "🤷 К сожалению, я пока умею понимать только текст. Напишите мне словами, и я постараюсь помочь!"
    )


async def main():
    load_phrases()

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    timeout = ClientTimeout(total=100)
    session = aiohttp.ClientSession(timeout=timeout)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запуск бота"),
            BotCommand(command="clear_history", description="Очистить историю диалога"),
            BotCommand(command="info", description="Информация о боте"),
        ]
    )

    router.message.register(start_handler, Command("start"))
    router.message.register(
        partial(clear_history_handler, session=session),
        Command("clear_history"),
    )
    router.message.register(partial(info_handler), Command("info"))
    router.message.register(
        partial(message_handler, session=session, bot=bot),
        F.text,
    )

    # Остальные хэндлеры уже зарегистрированы декораторами
    dp.include_router(router)

    logging.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
