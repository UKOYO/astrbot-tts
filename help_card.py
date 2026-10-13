# -*- coding: utf-8 -*-
"""TTS 帮助与清单渲染卡片 —— 严谨瑞士风格（Swiss Grid Style）双列排版。

视觉规范：
  - 色彩方案：
      * #800020 (Burgundy / 勃艮第酒红): 主文字、主网格线、视觉焦点
      * #FFFFFF (White / 纯白): 区域底色、辅助框、标签底
      * #FFF9F2 (Ivory / 象牙白): 画布与卡片主背景
      * #D45060 (Coral Rose / 珊瑚绯红): 状态标签、强调点缀、高亮标记
  - 字体：OPPO Sans 4.0 (OPPOSans-4.0.ttf) + NotoColorEmoji.ttf
  - 结构：非对称瑞士网格，左侧音色列表，右侧语种列表，底部切换说明。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

import emoji as emoji_lib
from PIL import Image, ImageDraw, ImageFont

# 配色常量
COLOR_PRIMARY = (128, 0, 32)       # #800020 勃艮第酒红
COLOR_SECONDARY = (255, 255, 255)  # 纯白区域底色，避免卡片出现灰色块
COLOR_BG = (255, 249, 242)         # #FFF9F2 象牙白
COLOR_ACCENT = (212, 80, 96)       # #D45060 珊瑚绯红
COLOR_TAG = (250, 225, 230)        # 淡珊瑚标签底色

TEXT_MAIN = (128, 0, 32)
TEXT_BODY = (60, 20, 28)
TEXT_MUTED = (160, 110, 120)
GRID_LINE = (128, 0, 32)
GRID_LIGHT = (226, 210, 190)

F_SUPER = 38
F_TITLE = 24
F_SECTION = 20
F_BODY = 18
F_SMALL = 15
F_TINY = 13


@dataclass(slots=True)
class HelpCardTheme:
    resource_dir: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent / "resources"
    )
    font_name: str = "OPPOSans-4.0.ttf"
    emoji_font_name: str = "NotoColorEmoji.ttf"

    width: int = 960
    margin: int = 36
    padding: int = 32

    @property
    def font_path(self) -> Path:
        p = self.resource_dir / self.font_name
        if not p.exists():
            fallback = Path(r"E:\AstrBot\data\plugins\astrbot_plugin_box\core\resource\OPPOSans-4.0.ttf")
            if fallback.exists():
                return fallback
        return p

    @property
    def emoji_font_path(self) -> Path:
        p = self.resource_dir / self.emoji_font_name
        if not p.exists():
            fallback = Path(r"E:\AstrBot\data\plugins\astrbot_plugin_box\core\resource\NotoColorEmoji.ttf")
            if fallback.exists():
                return fallback
        return p


class HelpCardMaker:
    """瑞士国际主义风格 TTS 帮助卡片生成器。"""

    def __init__(self, theme: HelpCardTheme | None = None):
        self.theme = theme or HelpCardTheme()
        self._fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    def _font(self, size: int) -> ImageFont.FreeTypeFont:
        key = ("text", size)
        if key not in self._fonts:
            try:
                self._fonts[key] = ImageFont.truetype(str(self.theme.font_path), size)
            except Exception:
                self._fonts[key] = ImageFont.load_default()
        return self._fonts[key]

    def _emoji_font(self, size: int) -> ImageFont.FreeTypeFont:
        key = ("emoji", size)
        if key not in self._fonts:
            try:
                self._fonts[key] = ImageFont.truetype(str(self.theme.emoji_font_path), size)
            except Exception:
                self._fonts[key] = self._font(size)
        return self._fonts[key]

    def _measure(self, text: str, size: int) -> int:
        font = self._font(size)
        emoji_font = self._emoji_font(size)
        total = 0
        for char in text:
            if char in emoji_lib.EMOJI_DATA:
                total += int(emoji_font.getlength(char))
            else:
                total += int(font.getlength(char))
        return total

    def _draw_text(
        self,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        size: int,
        fill: tuple[int, ...],
    ) -> None:
        x, y = xy
        font = self._font(size)
        emoji_font = self._emoji_font(size)
        for char in text:
            if char in emoji_lib.EMOJI_DATA:
                draw.text((x, y + size // 4), char, font=emoji_font, fill=fill)
                x += int(emoji_font.getlength(char))
            else:
                draw.text((x, y), char, font=font, fill=fill)
                x += int(font.getlength(char))

    def render(
        self,
        *,
        current_lang: str = "标准日语（东京腔）",
        current_voice: str = "大魔王（默认）",
        languages: list[dict[str, str]] | None = None,
        voices: list[dict[str, str]] | None = None,
        date_str: str = "",
    ) -> bytes:
        """生成并返回卡片 PNG 字节流。"""
        if languages is None:
            languages = [
                {"alias": "日语", "name": "标准日语（东京腔）", "cmd": "tts 语种日语", "type": "内置"},
                {"alias": "关西腔", "name": "关西腔日语", "cmd": "tts 语种关西腔", "type": "内置"},
                {"alias": "中文", "name": "中文", "cmd": "tts 语种中文", "type": "内置"},
                {"alias": "粤语", "name": "粤语", "cmd": "tts 语种粤语", "type": "内置"},
                {"alias": "韩语", "name": "韩语", "cmd": "tts 语种韩语", "type": "内置"},
                {"alias": "英语", "name": "英语", "cmd": "tts 语种英语", "type": "内置"},
                {"alias": "德语", "name": "德语", "cmd": "tts 语种德语", "type": "内置"},
                {"alias": "法语", "name": "法语", "cmd": "tts 语种法语", "type": "内置"},
                {"alias": "西海岸", "name": "美国俚语西海岸", "cmd": "tts 语种西海岸", "type": "自定义"},
            ]
        if voices is None:
            voices = [
                {"name": "大魔王", "desc": "唯笑主音色（傲娇/沉静）", "cmd": "tts 音色大魔王", "tag": "默认"},
                {"name": "晓美焰", "desc": "清冷冷静声线", "cmd": "tts 音色晓美焰", "tag": "可用"},
                {"name": "Miku", "desc": "初音未来元气声线", "cmd": "tts 音色miku", "tag": "可用"},
                {"name": "Dio", "desc": "厚重磁性声线", "cmd": "tts 音色dio", "tag": "可用"},
                {"name": "全局默认", "desc": "恢复面板默认音色", "cmd": "tts 音色默认", "tag": "重置"},
            ]

        theme = self.theme
        width = theme.width
        margin = theme.margin
        pad = theme.padding

        card_w = width - margin * 2
        inner_w = card_w - pad * 2
        cx = margin + pad

        date_tag = date_str or date.today().strftime("%Y.%m.%d")

        # ---------------- 高度计算 ----------------
        top_h = 105
        status_bar_h = 56
        
        # 行高计算
        col_gap = 24
        half_w = (inner_w - col_gap) // 2
        
        voice_row_h = 42
        lang_row_h = 36
        
        left_body_h = 48 + len(voices) * voice_row_h + 16
        right_body_h = 48 + len(languages) * lang_row_h + 16
        columns_h = max(left_body_h, right_body_h)
        
        footer_tips_h = 120
        bottom_meta_h = 44

        total_inner_h = (
            top_h
            + status_bar_h
            + 20
            + columns_h
            + 20
            + footer_tips_h
            + bottom_meta_h
        )

        total_h = margin * 2 + total_inner_h

        # ---------------- 底板绘制 ----------------
        img = Image.new("RGBA", (width, total_h), COLOR_BG + (255,))
        draw = ImageDraw.Draw(img)

        # 纯直角主外框
        draw.rectangle(
            [margin, margin, margin + card_w, margin + total_inner_h],
            fill=COLOR_BG + (255,),
            outline=GRID_LINE,
            width=2,
        )

        # 顶部 8px 勃艮第红强调横梁
        draw.rectangle(
            [margin, margin, margin + card_w, margin + 8],
            fill=COLOR_PRIMARY + (255,),
        )

        y = margin + pad

        # ================= 1. 顶部 Header =================
        draw.rectangle([cx, y, cx + 58, y + 20], fill=COLOR_ACCENT + (255,))
        self._draw_text(draw, (cx + 8, y + 2), "HELP", F_TINY, (255, 255, 255))
        self._draw_text(
            draw,
            (cx + 68, y + 2),
            "TTS STUDIO // COMMAND SPECIFICATION & MATRIX",
            F_TINY,
            TEXT_MUTED,
        )

        date_text = f"SYS // {date_tag}"
        date_w = self._measure(date_text, F_TINY)
        self._draw_text(draw, (cx + inner_w - date_w, y + 2), date_text, F_TINY, TEXT_MAIN)

        y += 26
        self._draw_text(draw, (cx, y), "TTS 语音系统控制面板", F_SUPER, COLOR_PRIMARY)

        y += 50
        draw.line([(cx, y), (cx + inner_w, y)], fill=GRID_LINE, width=2)
        y += 14

        # ================= 2. 当前状态横条 =================
        draw.rectangle(
            [cx, y, cx + inner_w, y + 40],
            fill=COLOR_SECONDARY + (255,),
            outline=GRID_LIGHT,
            width=1,
        )
        self._draw_text(draw, (cx + 14, y + 10), "当前生效音色:", F_SMALL, TEXT_MUTED)
        self._draw_text(draw, (cx + 115, y + 9), current_voice, F_BODY, COLOR_PRIMARY)

        right_status_x = cx + half_w + col_gap + 14
        self._draw_text(draw, (right_status_x, y + 10), "当前生效语种:", F_SMALL, TEXT_MUTED)
        self._draw_text(draw, (right_status_x + 100, y + 9), current_lang, F_BODY, COLOR_PRIMARY)

        y += 56

        # ================= 3. 双列网格 (左: 音色 / 右: 语种) =================
        col_left_x = cx
        col_right_x = cx + half_w + col_gap
        col_top_y = y

        # 左列外框与标题
        draw.rectangle(
            [col_left_x, col_top_y, col_left_x + half_w, col_top_y + columns_h],
            fill=(255, 255, 255, 180),
            outline=GRID_LIGHT,
            width=1,
        )
        draw.rectangle(
            [col_left_x, col_top_y, col_left_x + half_w, col_top_y + 38],
            fill=COLOR_SECONDARY + (255,),
        )
        self._draw_text(draw, (col_left_x + 14, col_top_y + 8), "VOICE LIST // 可选音色列表", F_SECTION, COLOR_PRIMARY)

        # 绘制音色列表项
        vy = col_top_y + 48
        for v in voices:
            tag = v.get("tag", "可用")
            tag_bg = COLOR_ACCENT if tag in ("默认", "当前") else COLOR_SECONDARY
            tag_text_color = (255, 255, 255) if tag in ("默认", "当前") else TEXT_MAIN
            
            draw.rectangle([col_left_x + 14, vy + 4, col_left_x + 60, vy + 24], fill=tag_bg + (255,))
            self._draw_text(draw, (col_left_x + 20, vy + 5), tag, F_TINY, tag_text_color)
            
            self._draw_text(draw, (col_left_x + 70, vy + 3), v.get("name", ""), F_BODY, TEXT_BODY)
            
            cmd = v.get("cmd", "")
            cmd_w = self._measure(cmd, F_SMALL)
            self._draw_text(draw, (col_left_x + half_w - cmd_w - 14, vy + 5), cmd, F_SMALL, COLOR_ACCENT)
            
            draw.line([(col_left_x + 10, vy + 34), (col_left_x + half_w - 10, vy + 34)], fill=COLOR_SECONDARY, width=1)
            vy += voice_row_h

        # 右列外框与标题
        draw.rectangle(
            [col_right_x, col_top_y, col_right_x + half_w, col_top_y + columns_h],
            fill=(255, 255, 255, 180),
            outline=GRID_LIGHT,
            width=1,
        )
        draw.rectangle(
            [col_right_x, col_top_y, col_right_x + half_w, col_top_y + 38],
            fill=COLOR_SECONDARY + (255,),
        )
        self._draw_text(draw, (col_right_x + 14, col_top_y + 8), "LANGUAGE LIST // 支持语种列表", F_SECTION, COLOR_PRIMARY)

        ly = col_top_y + 46
        for l in languages:
            alias = l.get("alias", "")
            name = l.get("name", "")
            cmd = l.get("cmd", "")
            
            self._draw_text(draw, (col_right_x + 14, ly + 2), f"{alias} · {name}", F_SMALL, TEXT_BODY)
            
            cmd_w = self._measure(cmd, F_SMALL)
            self._draw_text(draw, (col_right_x + half_w - cmd_w - 14, ly + 2), cmd, F_SMALL, COLOR_ACCENT)
            
            draw.line([(col_right_x + 10, ly + 28), (col_right_x + half_w - 10, ly + 28)], fill=COLOR_SECONDARY, width=1)
            ly += lang_row_h

        y = col_top_y + columns_h + 18

        # ================= 4. 底部切换说明 =================
        draw.rectangle(
            [cx, y, cx + inner_w, y + footer_tips_h],
            fill=COLOR_SECONDARY + (255,),
            outline=GRID_LINE,
            width=1,
        )
        draw.rectangle([cx + 12, y + 10, cx + 110, y + 30], fill=COLOR_PRIMARY + (255,))
        self._draw_text(draw, (cx + 18, y + 12), "SWITCH GUIDE", F_TINY, (255, 255, 255))
        self._draw_text(draw, (cx + 120, y + 12), "常用切换指令速查（仅在当前私聊／群会话生效）", F_TINY, TEXT_MUTED)

        tips = [
            "▸ 切换音色：发送「tts 音色 大魔王」/「tts 音色 晓美焰」/「tts 音色 默认」（恢复全局）",
            "▸ 切换语种：发送「tts 语种 日语」/「tts 语种 关西腔」/「tts 语种 默认」（恢复全局）",
            "▸ 开关控制：发送「tts 开启」或「tts 关闭」控制语音生成",
        ]
        ty = y + 38
        for tip in tips:
            self._draw_text(draw, (cx + 16, ty), tip, F_SMALL, TEXT_BODY)
            ty += 24

        y += footer_tips_h + 16

        # ================= 5. 底部版权与规格 =================
        footer_left = "ASTRBOT TTS STUDIO // SWISS GRID DESIGN // OPPO SANS 4.0"
        self._draw_text(draw, (cx, y), footer_left, F_TINY, TEXT_MUTED)

        footer_right = "STATUS: ACTIVE // ISOLATED PER SESSION"
        fr_w = self._measure(footer_right, F_TINY)
        self._draw_text(draw, (cx + inner_w - fr_w, y), footer_right, F_TINY, TEXT_MUTED)

        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


def render_help_card(
    *,
    current_lang: str = "标准日语（东京腔）",
    current_voice: str = "大魔王（默认）",
    languages: list[dict[str, str]] | None = None,
    voices: list[dict[str, str]] | None = None,
) -> bytes:
    maker = HelpCardMaker()
    return maker.render(
        current_lang=current_lang,
        current_voice=current_voice,
        languages=languages,
        voices=voices,
    )
