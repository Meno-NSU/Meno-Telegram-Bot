import re
from typing import Final

from telegramify_markdown import customize, markdownify

TELEGRAM_ESCAPE_CHARS: Final[str] = r"_*[]()~`>#+-=|{}.!\\"


def escape_markdown_v2(text: str) -> str:
    """
    Escape Telegram MarkdownV2 special characters to keep parse_mode stable.
    """
    if not text:
        return ""
    return re.sub(f"([{re.escape(TELEGRAM_ESCAPE_CHARS)}])", r"\\\1", text)


def _replace_artifact_pairs(text: str) -> str:
    """
    Replace paired artifact markers like BOPEN ... BCLOSE with bold/italic markers.
    """
    pair_patterns = (
        (re.compile(r"@@BOPEN@@\s*(.*?)\s*@@BCLOSE@@", flags=re.DOTALL), r"**\1**"),
        (re.compile(r"BOPEN\s*(.*?)\s*BCLOSE", flags=re.DOTALL), r"**\1**"),
        (re.compile(r"@@IOPEN@@\s*(.*?)\s*@@ICLOSE@@", flags=re.DOTALL), r"*\1*"),
        (re.compile(r"IOPEN\s*(.*?)\s*ICLOSE", flags=re.DOTALL), r"*\1*"),
    )

    for pattern, replacement in pair_patterns:
        text = pattern.sub(replacement, text)
    return text


def sanitize_llm_artifacts(text: str) -> str:
    """
    Clean LLM placeholders that leak into responses.
    """
    if not text:
        return ""

    sanitized = _replace_artifact_pairs(text)

    single_replacements = {
        "@@BOPEN@@": "**",
        "@@BCLOSE@@": "**",
        "BOPEN": "**",
        "BCLOSE": "**",
        "@@IOPEN@@": "*",
        "@@ICLOSE@@": "*",
        "IOPEN": "*",
        "ICLOSE": "*",
    }
    for token, replacement in single_replacements.items():
        sanitized = sanitized.replace(token, replacement)

    sanitized = re.sub(r"@{0,2}(?:BOPEN|BCLOSE|IOPEN|ICLOSE)@{0,2}", "", sanitized)
    return sanitized


def prepare_stream_chunk(text: str) -> str:
    """
    Safe formatter for streaming chunks. No Markdown conversion, just escaping.
    """
    sanitized = sanitize_llm_artifacts(text)
    return escape_markdown_v2(sanitized)


def _strip_heading_padding(text: str) -> str:
    """
    telegramify-markdown prefixes headings with a leading space inside bold;
    trim that so headings render as regular bold lines.
    """
    return re.sub(r"(?m)^\*\s+(.*?)\*$", r"*\1*", text)


def prepare_final_message(text: str) -> str:
    """
    Convert raw Markdown text to Telegram-safe MarkdownV2.
    """
    sanitized = sanitize_llm_artifacts(text)

    try:
        cfg = customize.get_runtime_config().markdown_symbol
        cfg.head_level_1 = ""
        cfg.head_level_2 = ""
        cfg.head_level_3 = ""
        cfg.head_level_4 = ""

        converted = markdownify(sanitized, latex_escape=True)
        converted = _strip_heading_padding(converted)

        if any(token in converted for token in ("BOPEN", "BCLOSE", "@@BOPEN@@", "@@BCLOSE@@")):
            converted = sanitize_llm_artifacts(converted)
        return converted
    except Exception:
        return escape_markdown_v2(sanitized)
