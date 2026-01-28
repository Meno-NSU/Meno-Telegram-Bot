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
from aiogram.types import BotCommand, User
from aiohttp import ClientTimeout

from meno_telegram_bot.settings import settings
from meno_telegram_bot.utils.telegram_format import (
    escape_markdown_v2,
    prepare_final_message,
    prepare_stream_chunk,
    sanitize_llm_artifacts,
)

logging.basicConfig(level=logging.INFO)
router = Router()
pending_users = set()

# FIXME: Bad id
last_typing_times: defaultdict[int, float] = defaultdict(lambda: 0)
TYPING_INTERVAL = 4

# FIXME: Bad id
last_edit_times: defaultdict[int, float] = defaultdict(lambda: 0.0)
MIN_EDIT_INTERVAL: float = 0.8

# FIXME: Bad id
dialog_histories: defaultdict[int, list] = defaultdict(list)
MAX_HISTORY_MESSAGES: int = 12

PHRASES = {
    "thinking": ["Печатаю ответ..."],
    "fallback": ["Не удалось получить ответ."]
}

MESSAGES = {}

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


def strip_think_from_text(text: str) -> str:
    if THINK_OPEN not in text:
        return text

    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

    if THINK_OPEN in cleaned:
        cleaned = cleaned.split(THINK_OPEN, 1)[0]

    return cleaned.strip()


def load_phrases(path: str = "src/meno_telegram_bot/data/phrases.json"):
    global PHRASES
    try:
        with open(path, "r", encoding="utf-8") as f:
            PHRASES = json.load(f)
    except Exception as e:
        logging.warning(f"Не удалось загрузить фразы из {path}: {e}")


def load_messages(path: str = "src/meno_telegram_bot/data/messages.json"):
    global MESSAGES
    try:
        with open(path, "r", encoding="utf-8") as f:
            MESSAGES = json.load(f)
    except Exception as e:
        logging.warning(f"Не удалось загрузить сообщения из {path}: {e}")


def random_phrase(category: str) -> str:
    return random.choice(PHRASES.get(category, ["..."]))


async def get_backend_response(payload: dict, session: aiohttp.ClientSession) -> str:
    """
    Нестриминговый запрос — OpenAI-совместимый /v1/chat/completions.
    """
    payload = {**payload, "stream": False}

    try:
        async with session.post(str(settings.backend_api_url), json=payload) as response:
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
                str(settings.backend_api_url),
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

                        try:
                            obj = json.loads(data_str)
                        except json.JSONDecodeError:
                            logging.warning(f"Не удалось распарсить JSON из SSE: {data_str!r}")
                            continue

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
    await message.answer(MESSAGES.get("start_message", "Привет!"))


async def process_backend(
        message: types.Message,
        session: aiohttp.ClientSession,
        msg_to_edit: types.Message,
        bot: Bot,
        stop_event: asyncio.Event | None = None,
):
    user: User | None = message.from_user
    if not user:
        logging.error("Message missing user or chat: %s", message)
        return
    user_id = getattr(user, "id", None)
    if user_id is None:
        logging.error("Message without id: %s", message)
        return
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
    final_answer: str | None = None
    first_answer_sent = False

    try:
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        logging.info(f"Отправка стримингового запроса на backend с payload: {payload}")

        async for piece in stream_backend_response(payload, session):
            if not piece:
                continue

            raw_answer += piece

            visible_candidate: str | None = None
            has_open = THINK_OPEN in raw_answer
            has_close = THINK_CLOSE in raw_answer

            if has_open and not has_close:
                before_think = raw_answer.split(THINK_OPEN, 1)[0]
                if before_think.strip():
                    visible_candidate = before_think
                else:
                    continue
            elif has_close:
                cleaned = strip_think_from_text(raw_answer)
                if not cleaned.strip():
                    continue
                visible_candidate = cleaned
            else:
                visible_candidate = raw_answer

            now = time.time()
            last_edit = last_edit_times[chat_id]

            if now - last_edit >= MIN_EDIT_INTERVAL:
                last_edit_times[chat_id] = now
                try:
                    if stop_event is not None and not first_answer_sent:
                        stop_event.set()
                        first_answer_sent = True
                    prepared = prepare_stream_chunk(visible_candidate)
                    await msg_to_edit.edit_text(prepared, parse_mode="MarkdownV2")
                except Exception as e:
                    logging.error(f"Ошибка форматирования / edit_text в стриме: {e}")
                    try:
                        fallback_chunk = escape_markdown_v2(sanitize_llm_artifacts(visible_candidate))
                        await msg_to_edit.edit_text(fallback_chunk, parse_mode="MarkdownV2")
                    except Exception as e2:
                        logging.error(f"Не удалось обновить сообщение без Markdown: {e2}")

        if not raw_answer.strip():
            logging.info("Стриминговый ответ пустой, делаем non-stream запрос")
            reply = await get_backend_response(payload, session)
            logging.warning(f"Non-stream ответ backend: {repr(reply)}")

            reply_clean = strip_think_from_text(reply)
            final_answer = sanitize_llm_artifacts(reply_clean)

            if stop_event is not None and not stop_event.is_set():
                stop_event.set()

            try:
                prepared = prepare_final_message(reply_clean)
                await msg_to_edit.edit_text(prepared, parse_mode="MarkdownV2")
            except Exception as e:
                logging.error(f"Ошибка форматирования MarkdownV2 (fallback): {e}")
                await msg_to_edit.edit_text(escape_markdown_v2(final_answer), parse_mode="MarkdownV2")
        else:
            answer_clean = strip_think_from_text(raw_answer)
            final_answer = sanitize_llm_artifacts(answer_clean)

            if stop_event is not None and not stop_event.is_set():
                stop_event.set()
            try:
                prepared = prepare_final_message(answer_clean)
                await msg_to_edit.edit_text(prepared, parse_mode="MarkdownV2")
            except Exception as e:
                logging.error(f"Ошибка финального форматирования MarkdownV2: {e}")
                await msg_to_edit.edit_text(escape_markdown_v2(final_answer), parse_mode="MarkdownV2")

    except Exception as e:
        logging.error(f"Ошибка при обработке запроса: {e}")
        try:
            fallback = random_phrase("fallback")
            final_answer = sanitize_llm_artifacts(final_answer) if final_answer else sanitize_llm_artifacts(fallback)
            if stop_event is not None and not stop_event.is_set():
                stop_event.set()
            await msg_to_edit.edit_text(escape_markdown_v2(final_answer), parse_mode="MarkdownV2")
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
            now: float = time.time()
            if now - last_typing_times[chat_id] >= TYPING_INTERVAL:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
                last_typing_times[chat_id] = now
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        pass


async def rotate_thinking_phrases(
        msg: types.Message,
        stop_event: asyncio.Event,
        interval: float = 4.0,
) -> None:
    try:
        while not stop_event.is_set():
            await asyncio.sleep(interval)
            if stop_event.is_set():
                break
            try:
                await msg.edit_text(random_phrase("thinking"))
            except Exception as e:
                logging.debug(f"Не удалось обновить thinking-сообщение: {e}")
    except asyncio.CancelledError:
        pass


async def message_handler(
        message: types.Message,
        session: aiohttp.ClientSession,
        bot: Bot,
):
    user: User | None = message.from_user
    if not user:
        logging.error("Message missing user or chat: %s", message)
        return
    user_id = getattr(user, "id", None)
    if user_id is None:
        logging.error("Message without id: %s", message)
        return

    if user_id in pending_users:
        await message.answer(
            MESSAGES.get("wait_previous_request", "⏳ Пожалуйста, дождитесь ответа на предыдущий запрос."))
        return

    chat_id = message.chat.id

    pending_users.add(user_id)

    thinking_msg = await message.answer(random_phrase("thinking"))

    stop_event = asyncio.Event()

    typing_task = asyncio.create_task(keep_typing(bot, chat_id))
    rotating_task = asyncio.create_task(rotate_thinking_phrases(thinking_msg, stop_event))
    backend_task = asyncio.create_task(process_backend(message, session, thinking_msg, bot, stop_event))

    try:
        await backend_task
    except Exception as e:
        logging.error(f"Ошибка в message_handler: {e}")
    finally:
        stop_event.set()
        typing_task.cancel()
        rotating_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task
        with contextlib.suppress(asyncio.CancelledError):
            await rotating_task
        pending_users.discard(user_id)


async def clear_history_handler(message: types.Message, session: aiohttp.ClientSession):
    chat_id = message.chat.id
    dialog_histories.pop(chat_id, None)

    reset_url = f"{settings.backend_api_url}/clear_history"
    payload = {"chat_id": str(chat_id)}

    try:
        async with session.post(reset_url, json=payload) as response:
            if response.status == 200:
                await message.answer(MESSAGES.get("clear_history_success", "История очищена"))
            else:
                await message.answer(MESSAGES.get("clear_history_error", "Ошибка"))
    except Exception:
        logging.exception("Ошибка при очистке истории:")
        await message.answer(MESSAGES.get("clear_history_error", "Ошибка"))


async def info_handler(message: types.Message):
    await message.answer(MESSAGES.get("info_message", "Информация о боте"))


@router.message(F.sticker)
async def handle_sticker(message: types.Message):
    await message.answer(MESSAGES.get("media_handlers", {}).get("sticker", "Отправьте текст"))


@router.message(F.photo)
async def handle_photo(message: types.Message):
    await message.answer(MESSAGES.get("media_handlers", {}).get("photo", "Отправьте текст"))


@router.message(F.video)
async def handle_video(message: types.Message):
    await message.answer(MESSAGES.get("media_handlers", {}).get("video", "Отправьте текст"))


@router.message(F.voice)
async def handle_voice(message: types.Message):
    await message.answer(MESSAGES.get("media_handlers", {}).get("voice", "Отправьте текст"))


@router.message(F.video_note)
async def handle_video_note(message: types.Message):
    await message.answer(MESSAGES.get("media_handlers", {}).get("video_note", "Отправьте текст"))


@router.message(F.audio)
async def handle_audio(message: types.Message):
    await message.answer(MESSAGES.get("media_handlers", {}).get("audio", "Отправьте текст"))


@router.message(F.document)
async def handle_document(message: types.Message):
    await message.answer(MESSAGES.get("media_handlers", {}).get("document", "Отправьте текст"))


@router.message(F.animation)
async def handle_animation(message: types.Message):
    await message.answer(MESSAGES.get("media_handlers", {}).get("animation", "Отправьте текст"))


@router.message(F.contact)
async def handle_contact(message: types.Message):
    await message.answer(MESSAGES.get("media_handlers", {}).get("contact", "Отправьте текст"))


@router.message(F.location)
async def handle_location(message: types.Message):
    await message.answer(MESSAGES.get("media_handlers", {}).get("location", "Отправьте текст"))


@router.message(~F.text)
async def handle_unknown(message: types.Message):
    await message.answer(MESSAGES.get("media_handlers", {}).get("unknown", "Отправьте текст"))


async def main():
    load_phrases()
    load_messages()

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

    dp.include_router(router)

    logging.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
