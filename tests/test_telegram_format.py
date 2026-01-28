import re

from meno_telegram_bot.utils.telegram_format import (
    prepare_final_message,
    prepare_stream_chunk,
    sanitize_llm_artifacts,
)


def test_heading_rendered_as_bold():
    result = prepare_final_message("# Заголовок")
    assert re.search(r"\*Заголовок\*", result)


def test_bold_and_italic_preserved():
    assert "*bold*" in prepare_final_message("**bold**")
    assert "_italic_" in prepare_final_message("*italic*")
    assert "_italic_" in prepare_final_message("_italic_")


def test_llm_artifacts_removed_and_wrapped():
    result = prepare_final_message("4. @@BOPEN@@текст@@BCLOSE@@:")
    assert "@@BOPEN@@" not in result and "@@BCLOSE@@" not in result
    assert "*текст*" in result


def test_llm_plain_artifacts_stripped():
    result = prepare_stream_chunk("BOPEN Институты НГУ: BCLOSE")
    assert "BOPEN" not in result and "BCLOSE" not in result
    assert "\\*\\*Институты НГУ:\\*\\*" in result


def test_sanitize_llm_artifacts_basic():
    assert "BOPEN" not in sanitize_llm_artifacts("text BOPEN ok BCLOSE")


def test_special_characters_are_escaped():
    escaped = prepare_stream_chunk(". ! ( ) [ ]")
    assert "\\." in escaped and "\\!" in escaped
    assert "\\(" in escaped and "\\)" in escaped
    assert "\\[" in escaped and "\\]" in escaped
