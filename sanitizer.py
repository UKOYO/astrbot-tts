# -*- coding: utf-8 -*-
"""朗读文本净化。

把「能看的文本」变成「能读的文本」：画面描写、舞台指示、Markdown、颜文字
都不该进入声码器。判断顺序很重要——先切掉整块描写，再处理零散符号。

这一层刻意不依赖任何别的模块，方便单独跑测试。
"""

from __future__ import annotations

import re

# 画面描写轨：{ ... }（不嵌套，配合 DOTALL）
BRACE_BLOCK = re.compile(r"\{[^{}]*\}", re.S)

# 舞台指示：（揉了揉眼睛）【旁白】[动作]
PAREN_BLOCK = re.compile(r"[（(【\[][^）)】\]]{0,160}[）)】\]]")

# Markdown 与聊天排版
MARKDOWN_PATTERNS = (
    (re.compile(r"\*\*\*(.+?)\*\*\*", re.S), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", re.S), r"\1"),
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"`{1,3}([^`]+)`{1,3}"), r"\1"),
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),
    (re.compile(r"^\s{0,3}>\s?", re.M), ""),
    (re.compile(r"^\s{0,3}[-*+]\s+", re.M), ""),
    (re.compile(r"~~(.+?)~~", re.S), r"\1"),
    (re.compile(r"\|"), " "),
)

# 收尾残余的排版符号；波浪号是朗读滑音标记，不在这里删
STRAY_STARS = re.compile(r"[*#`^]+")

# 波浪号：滑音标记只保留一个，连续堆叠会让声码器啸叫
TILDE_RUN = re.compile(r"~{2,}")

# 省略号：停顿只留一组「……」
ELLIPSIS_RUN = re.compile(r"…{3,}")

# 颜文字与符号表情
KAOMOJI = re.compile(
    r"[（(][^）)]{0,12}[・´`＞><；;ㅠㅜω∀∇▽°)（][^）)]{0,12}[）)]"
    r"|[´`]?[（(][>＜<][^）)]{0,10}[）)]"
)

# Emoji / 其他符号平面字符
EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2190-\u21FF\u2B00-\u2BFF\uFE0F\u200D]"
)

# 连续空白与换行
WHITESPACE = re.compile(r"[ \t\u3000]+")
NEWLINES = re.compile(r"\n{2,}")

# 朗读前的标点收尾：声码器怕连续符号
DUP_PUNCT = re.compile(r"([，。！？、；：,.!?;:])\1+")
LEADING_PUNCT = re.compile(r"^[\s，。！？、；：,.!?;:…~—]+")
TRAILING_SPACE_BEFORE_PUNCT = re.compile(r"\s+([，。！？、；：,.!?;:])")


def strip_brace_blocks(text: str) -> str:
    """去掉 { } 生理／画面描写轨。"""
    return BRACE_BLOCK.sub("", text or "")


def strip_stage_directions(text: str) -> str:
    """去掉括号里的舞台指示与旁白。"""
    if not text:
        return ""
    prev = None
    out = text
    while prev != out:
        prev = out
        out = PAREN_BLOCK.sub("", out)
    return out


def strip_markup(text: str) -> str:
    """剥掉 Markdown、颜文字与 emoji。"""
    out = text or ""
    for pattern, repl in MARKDOWN_PATTERNS:
        out = pattern.sub(repl, out)
    out = KAOMOJI.sub("", out)
    out = EMOJI.sub("", out)
    out = STRAY_STARS.sub("", out)
    return out


def tidy_punctuation(text: str, *, max_repeat: int = 1) -> str:
    """收一下重复标点，删掉开头的孤立标点。

    TTS 前端遇到连续标点容易生成静音或电流声，这里只做保守清理，
    不主动改写用户看得见的正文。
    """
    out = text or ""
    out = DUP_PUNCT.sub(lambda m: m.group(1) * max_repeat, out)
    out = ELLIPSIS_RUN.sub("……", out)
    out = TILDE_RUN.sub("~", out)
    out = TRAILING_SPACE_BEFORE_PUNCT.sub(r"\1", out)
    out = LEADING_PUNCT.sub("", out)
    return out


def strip_for_speech(
    text: str,
    *,
    strip_parenthetical: bool = True,
    strip_emoji: bool = True,
    keep_newlines: bool = False,
    max_chars: int = 1200,
) -> str:
    """朗读稿净化主入口。

    Args:
        text: 原始回复正文。
        strip_parenthetical: 是否连括号里的日常补充一起删掉。
        strip_emoji: 是否删除颜文字与 emoji。
        keep_newlines: 保留换行（默认压成单行，避免 TTS 断成多段）。
        max_chars: 超长截断，防止单次合成过载。
    """
    out = strip_brace_blocks(text)
    if strip_parenthetical:
        out = strip_stage_directions(out)
    out = strip_markup(out) if strip_emoji else _strip_markdown_only(out)
    out = WHITESPACE.sub(" ", out)
    out = out if keep_newlines else out.replace("\n", " ")
    out = NEWLINES.sub("\n", out)
    out = tidy_punctuation(out).strip()
    if max_chars and len(out) > max_chars:
        out = out[:max_chars].rstrip() + "……"
    return out


def _strip_markdown_only(text: str) -> str:
    out = text or ""
    for pattern, repl in MARKDOWN_PATTERNS:
        out = pattern.sub(repl, out)
    return STRAY_STARS.sub("", out)
