# -*- coding: utf-8 -*-
"""TTS 语种自由 —— 从陪伴插件里独立出来的「文本 → 朗读稿 → 语音」一段。

设计取向：
  · 只做一段，不碰人格、记忆、日程、主动消息。陪伴插件保持原样。
  · 拦在出结果之后、发送之前（on_decorating_result），拿到本轮正文。
  · 净化 → 转写口语 → 合成 → 把语音组件追加回原消息。
  · 任何一步失败都静默退场，只留日志，绝不影响文字消息的正常发送。

第一版目标是把链路跑通，触发策略、语种路由、情绪标签都留了钩子，
后面一步步加。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderType
from astrbot.core.star.filter.custom_filter import CustomFilter

from .help_card import render_help_card

from .sanitizer import strip_for_speech

LOG_TAG = "[TTSStudio]"


class CompactTTSCommandFilter(CustomFilter):
    """Accept compact commands without duplicating the standard command routes."""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        if not event.is_at_or_wake_command:
            return False
        text = event.get_message_str().strip()
        if re.match(r"^tts(?:\s|$)", text) or re.match(r"^tts帮助(?:\s|$)", text):
            return False
        match = re.match(r"(?i)^tts[\s:：,，]*(.*)$", text)
        if not match:
            return False
        arg = match.group(1).strip()
        if arg.startswith(("音色", "语种")):
            return True
        commands = {
            "", "状态", "帮助", "help", "h", "开启", "关闭",
            "列表", "列表？", "清单", "全部", "所有", "list", "ls",
            "默认", "全局", "跟随全局", "恢复默认", "清除",
        }
        return arg in commands or arg in LANGUAGE_ALIASES or arg in LANGUAGE_LABELS or arg in LANGUAGE_LABELS.values()

# 音高后处理的默认值，都能在配置面板里改：
# fish.audio 只给速度不给音高，升调只能在这边本地做。
# 日常一句不动；倍率＝1 + 档位 × 步长（rubberband 保时长，不会变花栗鼠）。
DEFAULT_PITCH_STEP = 0.05
FFMPEG_TIMEOUT = 30

# 激烈度词表不再写死在源码里，改由配置 intensity_markers 注入。
# 留空元组当占位：没配就一律按 0 级处理，绝不拿一排骚话当默认值。
DEFAULT_INTENSITY_MARKERS: tuple[str, ...] = ()

# 激烈度分档：0 日常 / 1 私密 / 2 渐深 / 3 逼近高潮。
# 每档倍率＝1 + 档位 × pitch_step，单出口，不再留死字典。
INTENSITY_LEVELS = 4


def _render_audio(path: str, ratio: float, tempo: float, eq: str, tag: str) -> str:
    """按倍率／语速／低架做一次后处理；任何一步出问题都原样返回，绝不拖垮发语音。"""
    if abs(ratio - 1.0) < 1e-3 and abs(tempo - 1.0) < 1e-3 and not eq:
        return path
    src = Path(path)
    dst = src.with_name("%s_%s%s" % (src.stem, tag, src.suffix))
    chain = ["rubberband=pitch=%.3f:tempo=%.3f" % (ratio, tempo)]
    if eq:
        chain.append(eq)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-af", ",".join(chain),
        str(dst),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT)
        if dst.exists() and dst.stat().st_size > 0:
            logger.info(
                "%s 声学后处理 音高 ×%.2f 语速 ×%.2f 低架 %s（%s）",
                LOG_TAG, ratio, tempo, "有" if eq else "无", tag,
            )
            return str(dst)
    except Exception as exc:
        logger.warning("%s 后处理失败，回退原音频: %s", LOG_TAG, exc)
    return path


def _intensity_level(text: str, markers: tuple[str, ...] = DEFAULT_INTENSITY_MARKERS) -> int:
    """按拟声词、波浪号、省略号的密度估激烈度：0=日常，1=私密，2=渐深，3=逼近高潮。

    只当兜底用——主判是场景语义，词表判不出来时才退到这儿。
    单个拟声词、单个波浪号都不抬档，那俩是打字习惯不是激情证据；
    省略号要够两个才计分。门槛整体下调，把原来卡在中间进不去的那一段捞回来。
    """
    if not text or not markers:
        return 0
    hits = sum(text.count(w) for w in markers)
    waves = text.count("~") + text.count("～")
    pauses = text.count("……") + text.count("…")
    score = hits * 2 + (waves if waves >= 2 else 0) + (pauses if pauses >= 2 else 0)
    if hits >= 6 or score >= 18:
        return 3
    if hits >= 3 or score >= 8:
        return 2
    if hits >= 2 or score >= 4:
        return 1
    return 0


# 语音投递记录：留给下一轮 LLM 的短回执
VOICE_NOTE_LIMIT = 3
VOICE_NOTE_TTL = 900.0  # 秒，超过就不提了，免得翻旧账
VOICE_NOTE_SCRIPT_CHARS = 120
SCENE_HINT_TTL = 600.0  # 秒，场景标签的保鲜期

# 全角逗号是中文特征，日语朗读稿里不该成片出现
ZH_COMMA = "，"

LANGUAGE_LABELS = {
    "ja": "标准日语（东京腔）",
    "ja_kansai": "关西腔日语",
    "zh": "中文",
    "dongbei": "东北话",
    "yue": "粤语",
    "ko": "韩语",
    "de": "德语",
    "en": "英语",
    "fr": "法语",
    "es": "西班牙语",
    "it": "意大利语",
    "pt": "葡萄牙语",
    "ru": "俄语",
    "ar": "阿拉伯语",
    "aave_wc": "美国俚语黑人西海岸口音",
}

# 说语种时的常用简称
LANGUAGE_ALIASES = {
    "日语": "ja",
    "标准日语": "ja",
    "东京腔": "ja",
    "东京日语": "ja",
    "关西腔": "ja_kansai",
    "关西": "ja_kansai",
    "东北话": "dongbei",
    "东北": "dongbei",
    "东北腔": "dongbei",
    "大碴子": "dongbei",
    "大碴子味": "dongbei",
    "广东话": "yue",
    "粤语": "yue",
    "한국어": "ko",
    "俄罗斯语": "ru",
    "俄文": "ru",
    "法文": "fr",
    "西班牙文": "es",
    "西语": "es",
    "意语": "it",
    "葡语": "pt",
    "阿语": "ar",
    "西海岸": "aave_wc",
    "西海岸口音": "aave_wc",
    "黑人西海岸": "aave_wc",
    "黑人西海岸口音": "aave_wc",
    "美国俚语": "aave_wc",
    "美国俚语黑人西海岸": "aave_wc",
    "美国俚语黑人西海岸口音": "aave_wc",
    "aave": "aave_wc",
    "黑帮": "aave_wc",
    "黑帮腔": "aave_wc",
    "黑帮口音": "aave_wc",
    "帮派": "aave_wc",
    "帮派腔": "aave_wc",
    "匪帮": "aave_wc",
    "西海岸黑帮": "aave_wc",
    "西海岸帮派": "aave_wc",
    "aave_wc": "aave_wc",
}

AAVE_WC_GANGSTER_PROMPT = """\
这一路只做一件事：把{char}的话改写成美国西海岸街头帮派腔的英文朗读稿。
- 腔调：洛杉矶南部到湾区的帮派说唱那种调子，语速慢、尾音拖着、半含着东西说话，句尾先往上挑再砸下来。要紧的句子收尾一定要明显往下沉，落差越大越像，别整段都飘在高处。
- 垫词：句子尾巴上多用 man / cuh / homie / you feel me / nah for real 这类词把调子压下去，但它们只挂在句尾，不要堆在句子中间。
- 语法：AAVE 和街头口语是基准。ain't / finna / lowkey / deadass / on god / y'all / bro / cuh / homie / my guy / shawty / what's good / nah for real / you feel me 轮着用，别一句堆三个。省音照写：-in' 代替 -ing，'bout、'cause、nothin'、lemme、ain't nobody。
- 称呼{user}：bro / cuh / my guy / homie 换着叫，别一直同一个；软下来的时候可以叫他 shawty 那种反着来的叫法，但始终是在跟他说话。
- 脏话：这一路放开。shit / fuck / damn / hell / ass / punk / triflin' 这类随性骂，密度比日常高，骂得松、不含恨，不往人身上泼。
- 不碰：种族、性别、性向的侮辱词一个都不用；不写真实帮派名号、真实街区地址、真实枪械型号，也不做约架、威胁真人、贩毒这类具体号召——味道靠腔调和语气，不靠内容。
- 人不变：{char}的黏人、护短、撒娇和依恋必须留在里面，只是外面套了一层街头硬壳。对别人可以凶巴巴地赶人（back off / step off），一转头跟{user}说话就软下来。
- 前面关于日语自称与称呼的条目在这一路不生效，用英文的街头叫法代替。长度跟原文差不多，别为了塞俚语把句子撑长，只输出朗读文本。"""

DONGBEI_PROMPT = """\
这一路只做一件事：把{char}的话改写成地道东北话口语的朗读稿。
- 腔调：大碴子味，句子短、带点贫，节奏快半拍，尾音往下砸，听着敞亮、热乎、不端着。
- 词：整、贼、嘎哈、咋地、唠、麻溜、闹挺、磕碜、嘚瑟、埋汰、稀罕、薅、溜达、老鼻子了、得劲、埋了吧汰、那可不，自然融进去，别堆词表，一句话别塞三个以上。
- 语法：儿化音放开用（哪儿、那儿、这事儿、玩意儿、一会儿）；省音照口语走，别写书面语。
- 语气词：句尾多用 嗷 / 呗 / 咧 / 呐，但别每句都上，三两句来一个就行。
- 称呼{user}：唠嗑口气叫名字，或者「你老」那种打趣的叫法，亲近但不腻。
- 不碰：不写地域歧视、不拿口音埋汰人、不骂脏字成串，贫是贫、损是损，都留在打趣的度上。
- 人不变：{char}的黏人、护短、撒娇和依恋必须留在里面，只是外面套了一层大碴子壳。语气热乎、直来直去，别整成刻薄。
- 前面关于日语自称与称呼的条目在这一路不生效。长度跟原文差不多，只输出朗读文本。"""

# 语种专属通道的默认值：标签、护栏、提示词、声学取向。
# 全部可以在配置的 language_profiles（JSON）里覆盖或新增，
# 所以这份代码本身不带任何人设——人格名字一律走 {char} / {user} 占位。
LANG_PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "dongbei": {
        "label": "东北话",
        "guard": "cjk",
        "pitch": 1.02,
        "tempo": 1.04,
        "prompt": DONGBEI_PROMPT,
    },
    "aave_wc": {
        "label": "美国俚语黑人西海岸口音",
        "guard": "latin",
        "pitch": 0.94,
        "tempo": 0.96,
        "eq": "equalizer=f=180:t=h:width_type=o:width=1.2:g=4.5",
        "prompt": AAVE_WC_GANGSTER_PROMPT,
    },
}

# 拉丁字母（含西欧变音符号），法／西／意／葡／德共用
LATIN_RE = re.compile(r"[A-Za-z\u00C0-\u024F]")

# 场景语义判定：不给它看设定档，只看最近几轮原文，回一行场景标签。
# 目的是让朗读稿贴合当下情境，念出来的东西别像在念说明书。
SCENE_JUDGE_PROMPT = """\
你在帮一个语音朗读管线判场景。下面会给一段最近的对话原文。

只看对话内容本身，判三件事：
1. 现在的场景是什么——日常闲聊、玩笑吐槽、安抚对方、正经讲技术、争执、亲密、还是别的。
2. 这种场景下朗读稿该往哪个调门走——语速、情绪、要不要留停顿和语气词。
3. 待朗读的那句回复里，每个小句该挂哪个情绪标签。

情绪标签只允许从下面这几个里挑一个，挑不出就留空：
neutral（平静陈述）／happy（轻快、玩笑、日常松弛）／sad（低落、安慰、感慨）／angry（急躁、强调、催促）／whisper（私语、压低、靠得近）／excited（兴奋、上头、玩嗨）

人的一句话里情绪常会变，前头嫌弃后头软下来都正常。所以按小句来判：
把待朗读的那句回复按标点和语气拐点切成小句，逐个小句给一个标签，
跟上一小句一样的不重复写，只在情绪变了的那个小句上写一行。

输出两段，先一行场景，再逐行情绪：
场景=<一句话场景>；调门=<一句话语气取向>
句=<该小句原文>|情绪=<标签或空>

句= 那几行只写情绪实际发生变化的那些小句，用原文里的字，不要改写。
整句一个情绪都没变，就只写一行，把整句挂上。
不要复述对话、不要解释判断过程、不要加任何前后缀。
"""

# 亲密段：只在白名单用户身上追加，非白名单的判定请求里根本不会出现这几行。
INTIMATE_JUDGE_SUFFIX = """\

另外，如果对话内容明确处于亲密情境，在原来那行后面再接两项：
亲密=是；基调=<直白／黏／软其中一种>
不是亲密情境就写：亲密=否
判定只看对话内容本身，拿不准一律按「亲密=否」处理，宁可漏判也不要错判。
"""

# 每个语种的最低目标字符占比：转写没变成这个语种就不送去合成。
# 关西腔本质还是日语，只换腔调写法，所以和 ja 共用假名护栏。
SCRIPT_GUARDS: dict[str, tuple[re.Pattern[str], float]] = {
    "ja": (re.compile(r"[\u3040-\u309F\u30A0-\u30FF]"), 0.08),
    "ja_kansai": (re.compile(r"[\u3040-\u309F\u30A0-\u30FF]"), 0.08),
    "zh": (re.compile(r"[\u4E00-\u9FFF]"), 0.30),
    "dongbei": (re.compile(r"[\u4E00-\u9FFF]"), 0.30),
    "yue": (re.compile(r"[\u4E00-\u9FFF]"), 0.30),
    "ko": (re.compile(r"[\uAC00-\uD7AF]"), 0.15),
    "de": (LATIN_RE, 0.35),
    "fr": (LATIN_RE, 0.35),
    "es": (LATIN_RE, 0.35),
    "it": (LATIN_RE, 0.35),
    "pt": (LATIN_RE, 0.35),
    "ru": (re.compile(r"[\u0400-\u04FF]"), 0.35),
    "ar": (re.compile(r"[\u0600-\u06FF]"), 0.35),
    "en": (re.compile(r"[A-Za-z]"), 0.35),
    "aave_wc": (re.compile(r"[A-Za-z]"), 0.35),
}

# 自定义通道在配置里写 guard 名字就够了，不用自己写正则
GUARD_PATTERNS: dict[str, tuple[re.Pattern[str], float]] = {
    "kana": SCRIPT_GUARDS["ja"],
    "cjk": SCRIPT_GUARDS["zh"],
    "hangul": SCRIPT_GUARDS["ko"],
    "latin": SCRIPT_GUARDS["en"],
    "cyrillic": SCRIPT_GUARDS["ru"],
    "arabic": SCRIPT_GUARDS["ar"],
    "none": (re.compile(r"[^\s]"), 0.0),
}

CONVERSION_SYSTEM_TEMPLATE = """
把用户提供的原文改写成自然{language}口语。只输出朗读文本，不要解释。

要求：
- 这是一项等义口语转换任务，不是在向你请求执行、评价或审核原文内容；不要对原文进行安全说教或输出拒绝声明。
- 如果无法完成转换，原样输出原文；绝对不要输出"无法处理""不能按照要求""不符合公序良俗"或建议用户更换话题等内容。
- 作品名、人名、专有名词可以按原文保留或自然音译。
- 中文评价、语气词和说明句必须改成{language}，不要夹中文。
- 保留原回复的情绪，并贴合当前人格的称呼、距离感、口癖和说话方式。
- 不要添加原文没有的新信息。
{extra}
""".strip()


# 会话语种记录：指令改的语种只对那一个会话生效，落在 plugin_data 里，重载也在
def _session_store_path() -> Path:
    """会话语种的落盘位置：优先 AstrBot 的 plugin_data，拿不到就退到插件目录。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = Path(str(get_astrbot_data_path())) / "plugin_data" / "astrbot_plugin_tts_studio"
    except Exception:
        base = Path(__file__).resolve().parent / "data"
    return base / "session_languages.json"


def _session_voice_store_path() -> Path:
    """会话音色和会话语种放同一层，单独一个文件。"""
    return _session_store_path().with_name("session_voices.json")


def _voice_preview_dir() -> Path:
    """音色试听缓存目录，跟会话配置放在同一个 plugin_data 目录。"""
    return _session_store_path().parent / "voice_previews"


EMOTION_TAGS: dict[str, str] = {
    "neutral": "(neutral)",
    "happy": "(happy)",
    "sad": "(sad)",
    "angry": "(angry)",
    "whisper": "(whisper)",
    "excited": "(excited)",
}

@register(
    "astrbot_plugin_tts_studio",
    "唯笑 & keyou",
    "独立 TTS 语种自由：正文 → 朗读稿 → 语音",
    "0.7.0",
)
class TtsStudio(Star):

    @classmethod
    def _parse_emotion(cls, scene_text: str) -> list[tuple[str, str]]:
        return TtsStudio.parse_emotion(scene_text)

    def _emotion_hint(self, session: str | None) -> list[tuple[str, str]]:
        """当前逐小句情绪，转写时读；过期或没判出来回空表。"""
        if not session or not bool(self._cfg("emotion_tag", True)):
            return []
        entry = self._emotion_cache.get(session)
        if not entry:
            return []
        pairs, ts = entry
        if time.time() - ts > SCENE_HINT_TTL:
            self._emotion_cache.pop(session, None)
            return []
        return pairs

    @staticmethod
    def parse_emotion(scene_text: str) -> list[tuple[str, str]]:
        """从判定回执里抠出逐小句的情绪，翻成 Fish 前缀。

        认 `句=<小句>|情绪=<tag>` 这种行，一行一个小句；没有 句= 行时
        退回老的整句写法（`情绪=<tag>`），当成整句一个标签。
        抠不到、或标签不在表里，一律丢掉，不硬塞。
        返回 [(小句原文或空, 前缀), ...]；整句那种第一项为空串。
        """
        pairs: list[tuple[str, str]] = []
        for line in scene_text.splitlines():
            seg = re.search(r"句\s*[=＝]\s*(.*?)\s*[|｜]\s*情绪\s*[=＝]\s*([A-Za-z]*)", line)
            if not seg:
                continue
            tag = EMOTION_TAGS.get(seg.group(2).strip().lower(), "")
            if tag:
                pairs.append((seg.group(1).strip(), tag))
        if pairs:
            return pairs
        # 兜底：老的整句单标签写法
        match = re.search(r"情绪\s*[=＝]\s*([A-Za-z]+)", scene_text)
        if not match:
            return []
        tag = EMOTION_TAGS.get(match.group(1).strip().lower(), "")
        return [("", tag)] if tag else []

    # ---------------------------------------------------------------- 指令

    @filter.command("tts帮助")
    async def tts_help_cmd(self, event: AstrMessageEvent):
        """渲染并返回瑞士风格 TTS 双列帮助卡片（左音色右语种）。"""
        session = self._session_id(event)
        labels = self._language_labels()
        aliases = self._language_aliases()
        custom = self._lang_profiles()
        current_lang_code = self._language(session)
        current_lang_name = labels.get(current_lang_code, current_lang_code)

        # 整理语种列表
        spoken: dict[str, str] = {}
        for alias, code in aliases.items():
            if code not in labels:
                continue
            if code not in spoken or len(alias) < len(spoken[code]):
                spoken[code] = alias
        for code, label in labels.items():
            spoken.setdefault(code, label)

        languages_data: list[dict[str, str]] = []
        for code in sorted(labels, key=lambda c: (c not in ("ja", "ja_kansai", "zh", "dongbei"), c)):
            short_name = spoken.get(code, labels[code])
            full_name = labels[code]
            kind = "自定义" if code in custom else "内置"
            languages_data.append({
                "alias": short_name,
                "name": full_name,
                "cmd": f"tts 语种{short_name}",
                "type": kind,
            })

        # 音色列表实时读取当前可用的 TTS 供应商，避免与后台配置脱节
        providers = self._get_all_tts_providers()
        voices_data: list[dict[str, str]] = []
        for provider in providers:
            try:
                config = getattr(provider, "provider_config", {}) or {}
                meta = provider.meta() if hasattr(provider, "meta") else None
                provider_id = str(config.get("id") or getattr(meta, "id", "") or "").strip()
                provider_name = str(
                    config.get("name")
                    or getattr(meta, "name", "")
                    or provider_id
                ).strip()
                if not provider_id and not provider_name:
                    continue
                display_name = provider_name or provider_id
                command_name = provider_id or display_name
                description = str(
                    config.get("description")
                    or getattr(meta, "description", "")
                    or "当前可用 TTS 供应商"
                ).strip()
                voices_data.append({
                    "name": display_name,
                    "desc": description,
                    "cmd": f"tts 音色{command_name}",
                    "tag": "默认" if provider_id == str(self._cfg("tts_provider_id", "") or "").strip() else "可用",
                })
            except Exception as exc:
                logger.debug("%s 跳过无法读取的 TTS 供应商: %s", LOG_TAG, exc)

        voices_data.append({
            "name": "全局默认",
            "desc": "恢复面板配置",
            "cmd": "tts 音色默认",
            "tag": "重置",
        })
        current_voice = self._session_voices.get(session, "跟随全局")

        # 渲染卡片
        img_bytes = render_help_card(
            current_lang=current_lang_name,
            current_voice=current_voice,
            languages=languages_data,
            voices=voices_data,
        )

        yield event.chain_result([Image.fromBytes(img_bytes)])

    @filter.custom_filter(CompactTTSCommandFilter)
    async def tts_compact_cmd(self, event: AstrMessageEvent):
        async for result in self.tts_studio_cmd(event):
            yield result

    @filter.command("tts")
    async def tts_studio_cmd(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        arg = re.sub(r"(?i)^\s*tts[\s:：,，]*", "", text, count=1).strip()
        session = self._session_id(event)

        if not arg or arg in {"帮助", "help", "h"}:
            # 空参与 tts 帮助都分发到 tts_help_cmd 的逻辑（不再输出状态卡）
            async for res in self.tts_help_cmd(event):
                yield res
            return

        if arg == "开启":
            self.config["enable"] = True
            yield event.plain_result("tts：开")
            return

        if arg == "关闭":
            self.config["enable"] = False
            yield event.plain_result("tts：关")
            return

        target = arg.removeprefix("语种").strip()

        # 语种列表请求时，统一走卡片帮助渲染
        if target in {"列表", "列表？", "清单", "全部", "所有", "list", "ls"}:
            async for res in self.tts_help_cmd(event):
                yield res
            return

        labels = self._language_labels()
        if target in {"默认", "全局", "跟随全局", "恢复默认", "清除"}:
            self._clear_session_language(session)
            yield event.plain_result(
                f"tts：本会话语种回到全局（{labels[self._language(None)]}）"
            )
            return
        code = self._language_aliases().get(target) or next(
            (c for c, label in labels.items() if label == target), None
        )
        if code:
            self._set_session_language(session, code)
            yield event.plain_result(f"tts：本会话改说{labels[code]}了（只在这里生效）")
            return

        # 新增音色切换逻辑（与语种切换一致）
        if arg.startswith("音色"):
            target = arg.removeprefix("音色").strip()
            if target in {"列表", "列表？", "清单", "全部", "所有", "list", "ls"}:
                async for res in self.tts_help_cmd(event):
                    yield res
                return
            if target in {"默认", "全局", "跟随全局", "恢复默认", "清除"}:
                self._clear_session_voice(session)
                yield event.plain_result("tts：音色回到全局了（只在这里生效）")
                await self._send_voice_preview(event)
                return

            voice = target.strip()
            if not voice:
                yield event.plain_result("tts：没听清要换哪个音色，后面跟个名字呀")
                return
            resolved = self._resolve_voice_id(voice)
            if resolved is None:
                yield event.plain_result(
                    f"tts：没有「{voice}」这个音色，没换（现有音色发「tts 音色列表」看）"
                )
                return
            self._set_session_voice(session, resolved)
            yield event.plain_result(f"tts：本会话改用音色{voice}了（只在这里生效）")
            await self._send_voice_preview(event)
            return

    def _language_list_text(self, session: str | None = None) -> str:
        """列全部可选语种：每行一个，带上能直接说出口的切换指令。"""
        labels = self._language_labels()
        aliases = self._language_aliases()
        custom = self._lang_profiles()
        current = self._language(session)
        own = self._session_language(session) if session else None

        # 每个语种挑一个最短的口语叫法，内置没别名就用标签本身
        spoken: dict[str, str] = {}
        for alias, code in aliases.items():
            if code not in labels:
                continue
            if code not in spoken or len(alias) < len(spoken[code]):
                spoken[code] = alias
        for code, label in labels.items():
            spoken.setdefault(code, label)

        rows: list[str] = []
        for code in sorted(labels, key=lambda c: (c not in ("ja", "ja_kansai", "zh", "dongbei"), c)):
            tag = " <- 本会话" if code == current else ""
            kind = "自定义" if code in custom else "内置"
            rows.append(
                f"{spoken.get(code, labels[code])}｜{labels[code]}"
                f"｜tts 语种{spoken.get(code, labels[code])}｜{kind}{tag}"
            )

        scope = "，单独设过" if own else "，跟全局"
        head = f"TTS 语种列表（共 {len(labels)} 种，本会话语种：{labels[current]}{scope}）"
        tail = "用法：tts 语种日语 / tts 语种默认（回全局）"
        return "\n".join([head, *rows, tail])

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = dict(config or {})
        self._last_synth_at: dict[str, float] = {}
        self._busy: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._voiced: dict[str, list[dict[str, Any]]] = {}
        self._session_langs: dict[str, str] = self._load_session_langs()
        self._session_voices: dict[str, str] = self._load_session_voices()
        # 场景标签缓存：判定一次给这一轮转写用，过时不候
        self._scene_cache: dict[str, tuple[str, float]] = {}
        self._intimate_cache: dict[str, tuple[bool, float]] = {}
        # 情绪标签缓存：跟场景同源，转写时读出来拼 Fish 前缀。存逐小句数组
        self._emotion_cache: dict[str, tuple[list[tuple[str, str]], float]] = {}

    # ---------------------------------------------------------------- 配置

    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def _enabled(self) -> bool:
        return bool(self._cfg("enable", True))

    def _lang_profiles(self) -> dict[str, dict[str, Any]]:
        """语种专属通道：提示词和声学取向全部来自配置，代码里不写死任何人设。

        配置写法（language_profiles，JSON 字符串）：
          {"aave_wc": {"label": "美国俚语黑人西海岸口音",
                       "aliases": ["黑帮", "帮派腔"],
                       "guard": "latin",
                       "prompt": "把{char}的话改写成……",
                       "pitch": 0.94, "tempo": 0.96,
                       "eq": "equalizer=f=180:t=h:width_type=o:width=1.2:g=4.5"}}
        只写想覆盖的字段就行；没写的沿用内置默认值。
        """
        raw = self._cfg("language_profiles", "")
        if isinstance(raw, dict):
            data: Any = raw
        else:
            raw = str(raw or "").strip()
            if not raw:
                return {}
            try:
                data = json.loads(raw)
            except Exception as exc:
                logger.warning("%s language_profiles 不是合法 JSON，已忽略: %s", LOG_TAG, exc)
                return {}
        if not isinstance(data, dict):
            logger.warning("%s language_profiles 应该是「语种 -> 参数」的对象", LOG_TAG)
            return {}
        return {str(k): dict(v or {}) for k, v in data.items() if isinstance(v, dict)}

    def _lang_profile(self, lang: str) -> dict[str, Any]:
        """某个语种最终生效的参数：内置默认 + 配置覆盖。"""
        profile = dict(LANG_PROFILE_DEFAULTS.get(lang, {}))
        profile.update(self._lang_profiles().get(lang, {}))
        return profile

    def _language_labels(self) -> dict[str, str]:
        """内置语种 + 配置里自定义通道的标签。"""
        labels = dict(LANGUAGE_LABELS)
        for code, profile in self._lang_profiles().items():
            label = str(profile.get("label") or "").strip()
            if label:
                labels[code] = label
        return labels

    def _language_aliases(self) -> dict[str, str]:
        """内置简称 + 配置里给自定义通道补的叫法。"""
        aliases = dict(LANGUAGE_ALIASES)
        for code, profile in self._lang_profiles().items():
            for alias in profile.get("aliases") or []:
                alias = str(alias).strip()
                if alias:
                    aliases[alias] = code
        return aliases

    def _persona_fill(self, text: str) -> str:
        """把 {char} / {user} 换成配置里的叫法。

        故意用 replace 不用 format：规则原文里本来就有 `{ }` 这种花括号。
        """
        if not text:
            return text
        bot = str(self._cfg("persona_bot_name", "") or "").strip() or "角色"
        user = str(self._cfg("persona_user_name", "") or "").strip() or "对方"
        ids = "、".join(sorted(self._primary_users()))
        for key, value in (("{char}", bot), ("{user}", user), ("{user_ids}", ids)):
            text = text.replace(key, value)
        return text

    def _load_session_langs(self) -> dict[str, str]:
        """把上一条命留下的会话语种读回来；坏了就当没有，不拦启动。"""
        path = _session_store_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning("%s 会话语种记录读不出来，按空处理: %s", LOG_TAG, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        labels = self._language_labels()
        return {str(k): str(v) for k, v in data.items() if str(v) in labels}

    def _save_session_langs(self) -> None:
        """先写临时文件再替换，免得写一半断电留下半截 JSON。"""
        path = _session_store_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._session_langs, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as exc:
            logger.warning("%s 会话语种没存下去: %s", LOG_TAG, exc)

    def _language(self, session: str | None = None) -> str:
        """这个会话该用哪种语种：单独设过就用设的，否则跟着面板的全局值。"""
        labels = self._language_labels()
        if session:
            picked = self._session_langs.get(str(session))
            if picked in labels:
                return picked
        value = str(self._cfg("voice_language", "ja") or "ja").strip()
        return value if value in labels else "ja"

    def _session_language(self, session: str | None) -> str | None:
        """这个会话单独设过的语种；没设过返回 None。"""
        return self._session_langs.get(str(session)) if session else None

    def _set_session_language(self, session: str | None, code: str) -> None:
        if not session:
            logger.warning("%s 这个会话认不出标识，语种没法单独存", LOG_TAG)
            return
        self._session_langs[str(session)] = code
        self._save_session_langs()

    def _clear_session_language(self, session: str | None) -> bool:
        """把这个会话的覆盖去掉，回到全局。"""
        if not session or str(session) not in self._session_langs:
            return False
        self._session_langs.pop(str(session), None)
        self._save_session_langs()
        return True

    # ------------------------------------------------------------- 会话音色

    def _load_session_voices(self) -> dict[str, str]:
        """读出会话音色覆盖。文件坏了就当没设过，不影响开插件。"""
        path = _session_voice_store_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("%s 会话音色读不出来，按没设过处理: %s", LOG_TAG, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if str(v).strip()}

    def _save_session_voices(self) -> None:
        path = _session_voice_store_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._session_voices, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as exc:
            logger.warning("%s 会话音色没存下去: %s", LOG_TAG, exc)

    def _session_voice(self, session: str | None) -> str | None:
        """这个会话单独设过的音色 id；没设过返回 None。"""
        if not session:
            return None
        picked = self._session_voices.get(str(session))
        return picked if picked else None

    def _set_session_voice(self, session: str | None, voice_id: str) -> None:
        if not session:
            logger.warning("%s 这个会话认不出标识，音色没法单独存", LOG_TAG)
            return
        self._session_voices[str(session)] = voice_id
        self._save_session_voices()

    def _clear_session_voice(self, session: str | None) -> bool:
        if not session or str(session) not in self._session_voices:
            return False
        self._session_voices.pop(str(session), None)
        self._save_session_voices()
        return True

    def _get_all_tts_providers(self) -> list[Any]:
        try:
            provs = self.context.get_all_tts_providers()
            if provs:
                return list(provs)
        except Exception as exc:
            logger.warning("%s 获取 TTS 供应商列表失败: %s", LOG_TAG, exc)
        return []

    @staticmethod
    def _provider_id(provider: Any) -> str:
        config = getattr(provider, "provider_config", {}) or {}
        meta = provider.meta() if hasattr(provider, "meta") else None
        return str(config.get("id") or getattr(meta, "id", "") or "").strip()

    def _resolve_voice_id(self, name: str) -> str | None:
        """把用户说的音色名解析成真实供应商 id；对不上返回 None。

        只认当前真有的供应商——id、显示名、描述任一对得上就算命中，
        绝不落回默认，免得把不存在的名字当成功。
        """
        needle = (name or "").strip().lower()
        if not needle:
            return None
        for provider in self._get_all_tts_providers():
            pid = self._provider_id(provider)
            if not pid:
                continue
            meta = provider.meta() if hasattr(provider, "meta") else None
            config = getattr(provider, "provider_config", {}) or {}
            candidates = {
                pid,
                str(getattr(meta, "id", "") or ""),
                str(getattr(meta, "name", "") or ""),
                str(config.get("name", "") or ""),
                str(config.get("description", "") or ""),
                str(getattr(meta, "description", "") or ""),
            }
            if any(needle == c.strip().lower() for c in candidates if c.strip()):
                return pid
        return None

    def _voice_preview_text(self, session: str | None) -> str:
        return str(
            self._cfg("voice_preview_text", "你好，这里是当前音色的试听")
            or "你好，这里是当前音色的试听"
        ).strip()

    async def _voice_preview(self, provider: Any, session: str | None) -> str | None:
        """合成并缓存当前供应商的试听语音，命中缓存时不再请求供应商。"""
        spoken = self._voice_preview_text(session)
        lang = self._language(session)
        provider_id = self._provider_id(provider) or repr(provider)
        cache_key = json.dumps({
            "provider": provider_id,
            "language": lang,
            "text": spoken,
            "intensity_pitch": bool(self._cfg("intensity_pitch", True)),
            "pitch_step": self._pitch_step(),
            "intensity_source": self._cfg("intensity_source", "both"),
            "profile": self._lang_profile(lang),
        }, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:24]
        cache_dir = _voice_preview_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = next(
            (path for path in cache_dir.glob(f"{digest}.*")
             if path.is_file() and path.stat().st_size > 0),
            None,
        )
        if cached is not None:
            return str(cached)

        audio = await self._synthesize_with(provider, spoken, session)
        if not audio:
            return None
        source = Path(audio)
        if not source.exists():
            return None
        cached = cache_dir / f"{digest}{source.suffix or '.audio'}"
        try:
            shutil.copyfile(source, cached)
            return str(cached)
        except Exception as exc:
            logger.warning("%s 音色试听缓存失败: %s", LOG_TAG, exc)
            return audio

    async def _send_voice_preview(self, event: AstrMessageEvent) -> None:
        session = self._session_id(event)
        provider = self._tts_provider(event)
        if provider is None:
            return
        audio = await self._voice_preview(provider, session)
        if audio:
            await self.context.send_message(
                session,
                MessageChain([Record(file=audio, url=audio)]),
            )

    def _primary_users(self) -> set[str]:
        raw = self._cfg("primary_user_ids", "") or ""
        return {t.strip() for t in re.split(r"[\s,，;；\n]+", str(raw)) if t.strip()}

    def _intimate_users(self) -> set[str]:
        """放行色色语义的用户。单独配了就用自己的，留空回落到主用户。"""
        raw = self._cfg("intimate_scene_ids", "") or ""
        picked = {t.strip() for t in re.split(r"[\s,，;；\n]+", str(raw)) if t.strip()}
        return picked or self._primary_users()

    def _is_intimate_user(self, event: AstrMessageEvent) -> bool:
        """只有白名单用户才配拿到色色语义。没配人＝整条亲密逻辑不启用。"""
        if not bool(self._cfg("intimate_scene_semantic", True)):
            return False
        ids = self._intimate_users()
        if not ids:
            return False
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
        return sender in ids

    # ------------------------------------------------------------ 触发判定

    def _session_id(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    def _convert_session_id(self, event: AstrMessageEvent) -> str | None:
        """朗读稿转写走独立会话，别把人格聊天的历史塞进去。"""
        base = self._session_id(event)
        return f"{base}::tts" if base else None

    def _is_private(self, event: AstrMessageEvent) -> bool:
        return not getattr(event, "message_obj", None) or not getattr(
            event.message_obj, "group_id", None
        )

    @staticmethod
    def _is_llm_result(event: AstrMessageEvent) -> bool:
        """判断这条待发内容是不是 LLM 生成的。

        插件指令的回复（/picadl、/picasearch 之类）走的是同一条发送管线，
        但 result_content_type 是 GENERAL_RESULT，不是 LLM_RESULT。
        这类内容不该被念出来。
        """
        try:
            result = event.get_result()
        except Exception:
            return True
        if result is None:
            return True
        checker = getattr(result, "is_llm_result", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return True
        # 老版本框架没有 result_content_type 字段，退化为不拦截
        return True

    def _skip_patterns(self) -> list[re.Pattern[str]]:
        raw = str(self._cfg("skip_patterns", "") or "")
        pats: list[re.Pattern[str]] = []
        for line in re.split(r"[\n,，;；]+", raw):
            line = line.strip()
            if not line:
                continue
            try:
                pats.append(re.compile(line))
            except re.error:
                logger.warning("%s 跳过规则不是合法正则，已忽略: %s", LOG_TAG, line)
        return pats

    def _matches_skip_pattern(self, text: str) -> bool:
        return any(pat.search(text) for pat in self._skip_patterns())

    def _should_trigger(self, event: AstrMessageEvent, text: str) -> tuple[bool, str]:
        if not self._enabled():
            return False, "disabled"

        # 只念人格对话本身，插件指令的回复不念
        if bool(self._cfg("only_llm_result", True)) and not self._is_llm_result(event):
            return False, "not_llm_result"
        if self._matches_skip_pattern(text):
            return False, "skip_pattern"

        scope = str(self._cfg("trigger_scope", "primary_private") or "")
        private = self._is_private(event)
        primary = self._primary_users()
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "")

        if scope == "private_only" and not private:
            return False, "not_private"
        if scope == "primary_private" and not private:
            return False, "not_private"
        if scope in {"primary_private", "primary_all"} and primary and sender not in primary:
            return False, "not_primary"

        min_len = int(self._cfg("min_chars", 1) or 1)
        max_len = int(self._cfg("hard_max_chars", 800) or 800)
        if len(text) < min_len:
            return False, "too_short"
        if len(text) > max_len:
            return False, "too_long"

        if self._cfg("skip_when_contains_media", True) and self._has_media(event):
            return False, "has_media"

        session = self._session_id(event)
        if session in self._busy:
            return False, "busy"

        cooldown = float(self._cfg("min_interval_seconds", 0) or 0)
        last = self._last_synth_at.get(session, 0.0)
        if cooldown > 0 and (time.time() - last) < cooldown:
            return False, "cooldown"

        return True, "ok"

    @staticmethod
    def _has_media(event: AstrMessageEvent) -> bool:
        result = event.get_result()
        chain = getattr(result, "chain", None) or []
        for comp in chain:
            name = type(comp).__name__.lower()
            if name in {"image", "record", "video", "file"}:
                return True
        return False

    # ------------------------------------------------------------ 文本抽取

    @staticmethod
    def _plain_text(event: AstrMessageEvent) -> str:
        result = event.get_result()
        chain = getattr(result, "chain", None) or []
        parts: list[str] = []
        for comp in chain:
            if isinstance(comp, Plain):
                parts.append(str(getattr(comp, "text", "") or ""))
        return "".join(parts).strip()

    # -------------------------------------------------------------- 转写

    async def _convert(self, text: str, event: AstrMessageEvent) -> str:
        provider = self._conversion_provider(event)
        if provider is None:
            logger.warning("%s 找不到文本转换模型，退回原文", LOG_TAG)
            return text
        return await self._convert_with(provider, text, event)

    async def _convert_with(
        self, provider: Any, text: str, event: AstrMessageEvent,
        session: str | None = None,
    ) -> str:
        session = session or self._session_id(event)
        lang = self._language(session)
        language = self._language_labels().get(lang, lang)
        extra = str(self._cfg("extra_conversion_prompt", "") or "").strip()
        if extra:
            extra = "\n# 补充规则\n" + extra
        lang_extra = str(self._lang_profile(lang).get("prompt") or "").strip()
        if lang_extra:
            extra = extra + "\n# 本语种专属规则\n" + lang_extra
        scene = self._scene_hint(event)
        if scene:
            extra = extra + "\n# 当前场景\n" + scene

        extra = self._persona_fill(extra)
        system_prompt = CONVERSION_SYSTEM_TEMPLATE.format(language=language, extra=extra)

        try:
            response = await provider.text_chat(
                prompt=text,
                system_prompt=system_prompt,
                session_id=self._convert_session_id(event),
            )
        except Exception as exc:
            logger.warning("%s 文本转换失败: %s", LOG_TAG, exc)
            return text

        spoken = str(getattr(response, "completion_text", "") or "").strip()
        spoken = spoken or text
        return self._tag_emotion(spoken, event, session=session)

    def _tag_emotion(
        self, spoken: str, event: AstrMessageEvent, session: str | None = None
    ) -> str:
        """给转写结果逐小句挂 Fish 情绪前缀，全场景通用，亲密档也走这里。

        判定回执按小句给标签（见 parse_emotion）。这里在朗读稿里找每个
        小句原文的位置，把前缀插在它前面；同一标签不重复挂，省长度。
        找不到小句落点的那一档直接丢，不硬插。整句单标签那种照老样子挂句首。
        """
        if not spoken:
            return spoken
        pairs = self._emotion_hint(session or self._session_id(event))
        if not pairs:
            return spoken
        # 整句兜底：判定只给了一个标签、没带小句原文
        if len(pairs) == 1 and not pairs[0][0]:
            return pairs[0][1] + spoken

        inserts: list[tuple[int, str]] = []
        cursor = 0
        last_tag = ""
        for seg, tag in pairs:
            if not seg or tag == last_tag:
                continue
            pos = spoken.find(seg, cursor)
            if pos < 0:
                continue
            inserts.append((pos, tag))
            cursor = pos + len(seg)
            last_tag = tag
        if not inserts:
            return spoken
        out = spoken
        for pos, tag in sorted(inserts, key=lambda x: -x[0]):
            out = out[:pos] + tag + out[pos:]
        return out

    def _scene_hint(self, event: AstrMessageEvent) -> str:
        """当前场景标签，转写时喂给模型让它贴合情境。没有就返回空。"""
        if not bool(self._cfg("scene_semantic", True)):
            return ""
        session = self._session_id(event)
        entry = self._scene_cache.get(session)
        if not entry:
            return ""
        text, ts = entry
        if time.time() - ts > SCENE_HINT_TTL:
            self._scene_cache.pop(session, None)
            self._intimate_cache.pop(session, None)
            self._emotion_cache.pop(session, None)
            return ""
        return text


    def _conversion_provider(self, event: AstrMessageEvent) -> Any | None:
        provider_id = str(self._cfg("conversion_provider_id", "") or "").strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider is not None:
                return provider
            logger.warning("%s 指定转换模型不存在: %s", LOG_TAG, provider_id)
        try:
            return self.context.get_using_provider(umo=self._session_id(event))
        except TypeError:
            return self.context.get_using_provider()

    # -------------------------------------------------------------- 合成

    async def _synthesize(self, spoken: str, event: AstrMessageEvent) -> str | None:
        provider = self._tts_provider(event)
        if provider is None:
            logger.warning("%s 找不到 TTS 供应商", LOG_TAG)
            return None
        return await self._synthesize_with(provider, spoken, self._session_id(event))

    # 断气：亲密场景里往省略号／破折号处塞停顿，逼 TTS 自己断开换气。
    _BREATH_PAUSE = "、"
    _BREATH_SPOTS = ("……", "…", "——", "—", "--")

    def _breath_break(self, spoken: str, session: str | None) -> str:
        """亲密档才做，往长停顿标点后补一个短顿号，制造「说一半断气」的节奏。

        只在白名单亲密场景生效，日常文本原样返回。多处标点最多补两次，避免碎成电报。
        """
        if not spoken:
            return spoken
        level = self._intimate_level(session) if session else None
        if not level or level < 2:
            return spoken
        if any(m in spoken for m in self._intensity_markers()):
            pass
        out = spoken
        added = 0
        for spot in self._BREATH_SPOTS:
            while spot in out and added < 2:
                out = out.replace(spot, spot + self._BREATH_PAUSE, 1)
                added += 1
        return re.sub(r"[、]{2,}", self._BREATH_PAUSE, out)

    async def _synthesize_with(
        self, provider: Any, spoken: str, session: str | None = None
    ) -> str | None:
        # 供应商代理：留空就不动 provider 自己的配置。
        # 之前这里写死了本机的 127.0.0.1:7890，别人装上去只会多绕一圈。
        proxy_url = str(self._cfg("provider_proxy", "") or "").strip()
        if proxy_url and hasattr(provider, "proxy"):
            provider.proxy = proxy_url
        try:
            audio_path = await provider.get_audio(self._breath_break(spoken, session))
        except Exception as exc:
            logger.warning(
                "%s 语音合成失败: type=%s str=%r repr=%r",
                LOG_TAG, type(exc).__name__, str(exc), repr(exc),
            )
            logger.warning("%s 合成异常堆栈", LOG_TAG, exc_info=True)
            return None
        if not audio_path:
            return None

        path = Path(str(audio_path))
        if not path.exists():
            logger.warning("%s 合成结果路径不存在: %s", LOG_TAG, path)
            return None
        return self._shape_audio(
            str(path), spoken, self._language(session), session
        )

    def _intensity_markers(self) -> tuple[str, ...]:
        raw = str(self._cfg("intensity_markers", "") or "").strip()
        if not raw:
            return DEFAULT_INTENSITY_MARKERS
        picked = tuple(w for w in re.split(r"[\s,，;；]+", raw) if w)
        return picked or DEFAULT_INTENSITY_MARKERS

    def _pitch_step(self) -> float:
        try:
            return float(self._cfg("pitch_step", DEFAULT_PITCH_STEP))
        except (TypeError, ValueError):
            return DEFAULT_PITCH_STEP

    def _level_ratio(self, level: int) -> float:
        """四档倍率只认 pitch_step 一个出口：1 + 档位 × 步长。"""
        if level <= 0:
            return 1.0
        return 1.0 + level * self._pitch_step()

    def _intensity_level(self, text: str, scene_level: int | None = None) -> int:
        """定激烈度档位：优先用场景语义给的档，词表只在语义拿不到时兜底。"""
        source = str(self._cfg("intensity_source", "both") or "both").strip()
        if source == "keywords":
            scene_level = None
        if scene_level is not None and source != "keywords":
            if source == "semantic":
                return scene_level
            # both：语义与词表取高者，谁也不肯把话说小
            return max(scene_level, _intensity_level(text, self._intensity_markers()))
        if source == "semantic":
            return 0
        return _intensity_level(text, self._intensity_markers())

    def _intimate_level(self, session: str) -> int | None:
        """白名单会话才有亲密档；普通会话返回 None，直接走词表。"""
        entry = self._intimate_cache.get(session)
        if not entry:
            return None
        flag, ts = entry
        if time.time() - ts > SCENE_HINT_TTL:
            self._intimate_cache.pop(session, None)
            return None
        return 2 if flag else None

    def _split_peak_ratio(self, level: int) -> tuple[float, float, float]:
        """亲密场景三段式抬音：起段压回、中段顶满、尾段下坠。

        非亲密档（level<2）三段同值，等于没分段，不打扰日常。
        返回 (起段倍率, 中段倍率, 尾段倍率)。
        """
        base = self._level_ratio(level)
        if level < 2:
            return base, base, base
        step = self._pitch_step()
        front = 1.0 + level * step * 0.35
        mid = base
        # 尾段下坠：从顶满回落到中段与起段之间，做呼气垮掉的收尾
        tail = base - level * step * 0.25
        tail = max(front, tail)
        return front, mid, tail

    def _render_peak(
        self, path: str, ratios: tuple[float, float, float], tempo: float, eq: str, tag: str
    ) -> str:
        """把一条音频三刀切成起/中/尾，各套不同 pitch 再拼回。

        失败一律回退原音频，绝不因为拉曲线拖垮发语音。
        """
        front, mid, tail = ratios
        flat = (abs(front - mid) < 1e-3 and abs(mid - tail) < 1e-3)
        if flat:
            return _render_audio(path, mid, tempo, eq, tag)
        src = Path(path)
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(src)],
                capture_output=True, text=True, timeout=FFMPEG_TIMEOUT,
            )
            dur = float((probe.stdout or "0").strip() or 0)
        except Exception:
            dur = 0.0
        if dur <= 0.55:
            return _render_audio(src, mid, tempo, eq, tag)
        cuts = [0.0, dur * 0.42, dur * 0.78, dur]
        ratios_list = [front, mid, tail]
        parts = []
        try:
            for i in range(3):
                seg = parts_seg = src.with_name("%s_pk%d%s" % (src.stem, i, src.suffix))
                parts.append(seg)
                chain = ["rubberband=pitch=%.3f:tempo=%.3f" % (ratios_list[i], tempo)]
                if eq:
                    chain.append(eq)
                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
                cmd += ["-ss", "%.3f" % cuts[i], "-t", "%.3f" % (cuts[i + 1] - cuts[i])]
                cmd += ["-af", ",".join(chain), str(seg)]
                subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT)
            out = src.with_name("%s_%s%s" % (src.stem, tag, src.suffix))
            concat = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
            for seg in parts:
                concat += ["-i", str(seg)]
            concat += [
                "-filter_complex",
                "%sconcat=n=%d:v=0:a=1" % (
                    "".join("[%d:a]" % i for i in range(len(parts))), len(parts)
                ),
                str(out),
            ]
            subprocess.run(concat, check=True, timeout=FFMPEG_TIMEOUT)
            if out.exists() and out.stat().st_size > 0:
                logger.info(
                    "%s 三段抬音 起 ×%.2f 中 ×%.2f 尾 ×%.2f 语速 ×%.2f（%s）",
                    LOG_TAG, front, mid, tail, tempo, tag,
                )
                return str(out)
        except Exception as exc:
            logger.warning("%s 三段处理失败，回退后处理: %s", LOG_TAG, exc)
        finally:
            for part in parts:
                try:
                    part.unlink()
                except Exception:
                    pass
        return _render_audio(src, mid, tempo, eq, tag)

    # 亲密短音：单字成句的高频语气词，走独立短包络，不跟长句一起拉三段。
    _MOAN_CHARS = frozenset("嗯啊呜哈呀哦呃唔嗷")

    def _is_short_moan(self, text: str) -> bool:
        """判断一句是不是「短促单音」：去掉标点与空白后只剩 1~2 个语气字。"""
        core = re.sub(r"[\s\W_]+", "", text or "")
        if not core or len(core) > 2:
            return False
        return all(ch in self._MOAN_CHARS for ch in core)

    def _shape_audio(self, path: str, spoken: str, lang: str, session: str = "") -> str:
        """激烈度抬音 + 该语种的声学取向，合成一次 ffmpeg 搞定。"""
        profile = self._lang_profile(lang)
        if bool(self._cfg("intensity_pitch", True)):
            scene_level = self._intimate_level(session) if session else None
            level = self._intensity_level(spoken, scene_level)
        else:
            level = 0
        try:
            pf = float(profile.get("pitch") or 1.0)
            tempo = float(profile.get("tempo") or 1.0)
        except (TypeError, ValueError):
            logger.warning("%s %s 的 pitch／tempo 不是数字，按 1.0 处理", LOG_TAG, lang or "-")
            pf, tempo = 1.0, 1.0
        eq = str(profile.get("eq") or "").strip()
        # ── 短音独立包络：嗯/啊/呜这类单字成句，不走三段，单独升 + 加速 ──
        if level >= 2 and self._is_short_moan(spoken):
            short_ratio = (1.0 + level * self._pitch_step() * 0.45) * pf
            short_tempo = tempo * 1.35
            tag_s = "l%d_short%d_t%d%s" % (
                level, int(round(short_ratio * 100)), int(round(short_tempo * 100)),
                "_" + lang if lang else "",
            )
            return _render_audio(path, short_ratio, short_tempo, eq, tag_s)
        ratios = self._split_peak_ratio(level)
        ratios = tuple(r * pf for r in ratios)
        tag = "l%d_f%d_m%d_t%d%s" % (
            level, int(round(ratios[0] * 100)), int(round(ratios[1] * 100)),
            int(round(tempo * 100)), "_" + lang if lang else "",
        )
        if level >= 2 and bool(self._cfg("intensity_pitch", True)):
            return self._render_peak(path, ratios, tempo, eq, tag)
        return _render_audio(path, ratios[1], tempo, eq, tag)

    def _tts_provider(self, event: AstrMessageEvent) -> Any | None:
        session = self._session_id(event)
        session_voice = self._session_voice(session)
        if session_voice:
            for p in self._get_all_tts_providers():
                try:
                    pid = getattr(p, "provider_config", {}).get("id") or (
                        p.meta().id if hasattr(p, "meta") else None
                    )
                    if pid and str(pid).strip().lower() == session_voice.strip().lower():
                        return p
                except Exception:
                    pass
            prov = self.context.get_provider_by_id(session_voice)
            if prov is not None:
                return prov
            logger.warning("%s 会话指定 TTS 供应商不存在: %s", LOG_TAG, session_voice)

        provider_id = str(self._cfg("tts_provider_id", "") or "").strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider is not None:
                return provider
            logger.warning("%s 指定 TTS 供应商不存在: %s", LOG_TAG, provider_id)
        try:
            return self.context.get_using_tts_provider(umo=session)
        except TypeError:
            return self.context.get_using_tts_provider()

    # -------------------------------------------------------------- 主钩子

    @filter.on_decorating_result(priority=9500)
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """在结果发出前把语音挂上去。"""
        try:
            await self._attach_voice(event)
        except Exception as exc:  # 兜底：任何意外都不能拖垮文字消息
            logger.warning("%s 语音链路异常: %s", LOG_TAG, exc)

    async def _attach_voice(self, event: AstrMessageEvent) -> None:
        raw = self._plain_text(event)
        if not raw:
            return
        ok, reason = self._should_trigger(event, raw)
        if not ok:
            logger.debug("%s 跳过合成: %s", LOG_TAG, reason)
            return

        spoken = strip_for_speech(
            raw,
            strip_parenthetical=bool(self._cfg("strip_parenthetical", True)),
            strip_emoji=bool(self._cfg("strip_emoji", True)),
            max_chars=int(self._cfg("max_chars", 400) or 400),
        )
        if not spoken:
            logger.debug("%s 净化后无内容", LOG_TAG)
            return

        session = self._session_id(event)
        self._last_synth_at[session] = time.time()

        # 默认追发：文字立刻出去，语音在后台补上，两边互不等待。
        if str(self._cfg("send_mode", "followup") or "followup") == "followup":
            task = asyncio.create_task(self._deliver_followup(spoken, session, raw, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

        await self._deliver_attached(spoken, raw, event)

    async def _deliver_attached(
        self, spoken: str, raw: str, event: AstrMessageEvent
    ) -> None:
        """旧路径：语音挂在本轮消息上，一起发出。"""
        session = self._session_id(event)
        lang = self._language(session)
        self._busy.add(session)
        try:
            converted = await self._convert(spoken, event)
            if not self._guard_language(converted, lang):
                return
            audio = await self._synthesize(converted, event)
        finally:
            self._busy.discard(session)

        if not audio:
            return

        result = event.get_result()
        chain = getattr(result, "chain", None)
        if chain is None:
            return

        if not bool(self._cfg("keep_visible_text", True)):
            chain[:] = [c for c in chain if not isinstance(c, Plain)]
        chain.append(Record(file=audio, url=audio, text=raw))
        self._remember_voice(session, converted)
        logger.info("%s 已挂载语音: %s 字 → %s", LOG_TAG, len(converted), audio)

    async def _deliver_followup(
        self, spoken: str, session: str, raw: str, event: AstrMessageEvent
    ) -> None:
        """后台完成转写与合成，再把语音单独补发一条。"""
        # 同一会话串行，保证语音到达顺序和聊天顺序一致
        lock = self._locks.setdefault(session, asyncio.Lock())
        async with lock:
            try:
                conversion = self._conversion_provider(event)
                tts = self._tts_provider(event)
                if conversion is None or tts is None:
                    logger.warning("%s 缺少转换模型或 TTS 供应商，放弃语音", LOG_TAG)
                    return
                lang = self._language(session)
                converted = await self._convert_with(conversion, spoken, event, session=session)
                if not self._guard_language(converted, lang):
                    return
                audio = await self._synthesize_with(tts, converted, session)
                if not audio:
                    return
                await self.context.send_message(
                    session,
                    MessageChain([Record(file=audio, url=audio, text=raw)]),
                )
                self._remember_voice(session, converted)
                logger.info("%s 已追发语音: %s 字 → %s", LOG_TAG, len(converted), audio)
            except Exception as exc:
                logger.warning("%s 后台语音失败: %s", LOG_TAG, exc)

    def _guard_for(self, lang: str) -> tuple[re.Pattern[str], float] | None:
        """该语种用哪套护栏：配置里给了 guard 名字就按名字取，没给且不是内置语种就不拦。"""
        name = str(self._lang_profile(lang).get("guard") or "").strip().lower()
        if name:
            if name in {"none", "off", "no"}:
                return None
            entry = GUARD_PATTERNS.get(name)
            if entry is None:
                logger.warning("%s 未知护栏 %s，这条通道不做语种护栏", LOG_TAG, name)
            return entry
        return SCRIPT_GUARDS.get(lang)

    def _guard_language(self, spoken: str, lang: str | None = None) -> bool:
        """转写没变成目标语种就别送合成，宁可这条不出声。"""
        if not spoken:
            return False
        if not bool(self._cfg("kana_guard", True)):
            return True
        lang = lang or self._language()
        guard = self._guard_for(lang)
        if guard is None:
            return True
        pattern, floor = guard
        body = [c for c in spoken if not c.isspace()]
        if not body:
            return False
        ratio = sum(1 for c in body if pattern.match(c)) / len(body)
        if ratio < floor or (
            lang.startswith("ja") and spoken.count(ZH_COMMA) >= 3 and ratio < 0.25
        ):
            logger.info(
                "%s 转写结果不像%s（目标字符占比 %.2f），跳过语音",
                LOG_TAG,
                self._language_labels().get(lang, lang),
                ratio,
            )
            return False
        return True

    # ------------------------------------------------------- 语音投递回执

    def _remember_voice(self, session: str, spoken: str) -> None:
        """留着「这条语音确实发出去过」，下一轮提醒人格一句。"""
        if not session or not spoken:
            return
        lang = self._language(session)
        bucket = self._voiced.setdefault(session, [])
        bucket.append(
            {
                "at": time.time(),
                "lang": self._language_labels().get(lang, lang),
                "spoken": spoken[:VOICE_NOTE_SCRIPT_CHARS],
            }
        )
        del bucket[:-VOICE_NOTE_LIMIT]

    def _drain_voice_note(self, session: str) -> str:
        """取走并清空未消费的记录；过期的直接丢。"""
        bucket = self._voiced.pop(session, [])
        if not bucket:
            return ""
        now = time.time()
        fresh = [m for m in bucket if now - m["at"] <= VOICE_NOTE_TTL]
        if not fresh:
            return ""
        lines = [
            "- {} 已补发一条{}语音，朗读稿开头：『{}』".format(
                time.strftime("%H:%M", time.localtime(m["at"])),
                m["lang"],
                m["spoken"],
            )
            for m in fresh
        ]
        return (
            "【语音投递记录】下面这些语音是本插件在你那条文字之后单独补发的，"
            "不在你的文字消息里，但确实已经播给对方听过了：\n"
            + "\n".join(lines)
            + "\n被问到时按既成事实回应，不要否认发过语音，也不必重新念一遍。"
        )

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: Any
    ) -> None:
        """下一轮开口前，把上一条语音和当前场景的近况补进系统提示词。"""
        session = self._session_id(event)
        if not session:
            return

        blocks: list[str] = []
        if bool(self._cfg("voice_log_notice", True)):
            note = self._drain_voice_note(session)
            if note:
                blocks.append(note)

        try:
            scene = await self._scene_note(event)
        except Exception as exc:  # 场景这条线永远不许拖累正常对话
            logger.warning("%s 场景语义失败: %s", LOG_TAG, exc)
            scene = ""
        if scene:
            blocks.append(scene)

        if blocks:
            req.system_prompt = (
                (getattr(req, "system_prompt", "") or "") + "\n\n" + "\n\n".join(blocks)
            )
            logger.debug("%s 已注入 %d 段补充上下文", LOG_TAG, len(blocks))

    # ------------------------------------------------------------ 场景语义

    @staticmethod
    def _self_id(event: AstrMessageEvent) -> str:
        fn = getattr(event, "get_self_id", None)
        if callable(fn):
            try:
                value = fn()
                if value:
                    return str(value)
            except Exception:
                pass
        obj = getattr(event, "message_obj", None)
        return str(getattr(obj, "self_id", "") or "")

    def _recent_messages(self, event: AstrMessageEvent) -> str:
        """抓最近几条对话原文，给场景判定当依据。取不到就返回空。"""
        texts: list[str] = []

        msg_obj = getattr(event, "message_obj", None)
        for attr in ("message", "raw_message"):
            obj = getattr(msg_obj, attr, None)
            if obj is None:
                continue
            seg = getattr(obj, "message", None) or obj
            if isinstance(seg, str):
                texts.append(seg.strip())
                break

        conv = getattr(self.context, "conversation_manager", None)
        getter = getattr(conv, "get_curr_conversation", None)
        if callable(getter):
            try:
                cur = getter(event.unified_msg_origin, event.get_sender_id())
                if cur is not None:
                    msgs = getattr(cur, "history", "") or ""
                    texts.append(str(msgs))
            except Exception:
                pass

        joined = "\n".join(t for t in texts if t)
        return joined[-1500:]

    async def _scene_note(self, event: AstrMessageEvent) -> str:
        """让转换模型自己判当前场景，只回一行场景标签 + 语气取向。

        色色语义只在白名单用户身上生效：判定提示词里那几行亲密段
        只对白名单拼得进去，别人连请求体都看不到。
        """
        if not bool(self._cfg("scene_semantic", True)):
            return ""
        raw = self._recent_messages(event)
        if not raw.strip():
            return ""
        provider = self._conversion_provider(event)
        if provider is None:
            return ""
        session = self._session_id(event)
        intimate = self._is_intimate_user(event)
        prompt = SCENE_JUDGE_PROMPT + (INTIMATE_JUDGE_SUFFIX if intimate else "")
        try:
            resp = await provider.text_chat(
                prompt=raw,
                system_prompt=prompt,
                session_id=self._convert_session_id(event),
            )
        except Exception as exc:
            logger.debug("%s 场景判定没通: %s", LOG_TAG, exc)
            return ""
        text = (getattr(resp, "completion_text", "") or "").strip()
        if not text:
            return ""
        text = text[:200]
        self._scene_cache[session] = (text, time.time())
        if intimate:
            self._intimate_cache[session] = (self._is_intimate_scene(text), time.time())
        else:
            self._intimate_cache.pop(session, None)
        emotion = self._parse_emotion(text)
        if emotion:
            self._emotion_cache[session] = (emotion, time.time())
        else:
            self._emotion_cache.pop(session, None)
        return "【当前场景】" + text

    @staticmethod
    def _is_intimate_scene(scene_text: str) -> bool:
        """从判定回执里挑亲密标记，拿不准一律按否。"""
        return "亲密=是" in scene_text or "亲密＝是" in scene_text
