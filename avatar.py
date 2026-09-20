# -*- coding: utf-8 -*-
"""头像线索：拼直链、拉图、缓存、指纹比对。

设计取向：
  · 不依赖 AstrBot 的任何东西，纯标准库 + Pillow，能单独跑测试。
  · OneBot 的事件里没有头像字段，QQ 头像走公开直链拼出来。
  · 拉回来的图按 QQ 存一份，再算一个感知指纹（dHash）：
    头像不常换，指纹没变就不重复花钱去描述。
  · 视觉转述交给调用方（main.py 那边有 provider），这里只负责「图」和「变没变」。
"""

from __future__ import annotations

import hashlib
import io
import json
import time
import urllib.request
from pathlib import Path
from typing import Any

AVATAR_DIR = "avatars"
INDEX_NAME = "index.json"
DEFAULT_SIZE = 640
# 多久重新拉一次图看有没有换头像。头像不常换，但拉一张几十 KB 不算贵，
# 15 分钟够快能当天发现，又不会每句话都去打人家 CDN。
REFRESH_SECONDS = 900
# 两张指纹差多少个 bit 以内算同一张头像。
# 头像被 CDN 重新压一次可能动一两个 bit，严丝合缝地比对会天天误报「换头像」。
MAX_DRIFT = 6
# 描述没成功时的补考次数。网络抖一下不该让一张脸永远没描述。
MAX_DESC_TRIES = 3

# 两条直链都是公开的，第一条不通就退第二条。
AVATAR_URLS = (
    "https://q1.qlogo.cn/g?b=qq&nk={qq}&s={size}",
    "https://q.qlogo.cn/headimg_dl?dst_uin={qq}&spec={size}&img_type=jpg",
)

UA = "Mozilla/5.0 (compatible; AstrBot-TTSStudio/0.8)"

# 交给视觉模型时的提示词：只要外观，不做身份判断。
DESCRIBE_PROMPT = (
    "这是一个人的聊天头像。请用不超过 80 个中文字描述它看起来是什么："
    "是插画、照片、动物、表情包还是纯色块，主色调是什么，整体给人什么感觉。"
    "不要推测性别、年龄、职业，也不要编造人物关系。直接说画面本身。"
)


def avatar_url(qq: str | int, size: int = DEFAULT_SIZE, index: int = 0) -> str:
    """拼一条头像直链，index=1 时用备用域名。"""
    return AVATAR_URLS[index % len(AVATAR_URLS)].format(qq=str(qq).strip(), size=size)


def _http_get(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    if not data:
        raise ValueError("空响应")
    return data


def fetch_avatar(qq: str | int, size: int = DEFAULT_SIZE, timeout: float = 20.0) -> tuple[bytes, str]:
    """按顺序试两条直链，返回 (图片字节, 用上的地址)。"""
    last: Exception | None = None
    for i in range(len(AVATAR_URLS)):
        url = avatar_url(qq, size, i)
        try:
            return _http_get(url, timeout), url
        except Exception as exc:  # 换下一条
            last = exc
    raise RuntimeError(f"头像拉取失败: {last}")


def fingerprint(data: bytes, hash_size: int = 8) -> str:
    """感知指纹：dHash。图被轻微重新压缩也认得出是同一张。"""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("L").resize(
            (hash_size + 1, hash_size), Image.LANCZOS
        )
        px = list(img.getdata())
        bits = 0
        for row in range(hash_size):
            base = row * (hash_size + 1)
            for col in range(hash_size):
                bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
        return f"d{bits:0{hash_size * hash_size // 4}x}"
    except Exception:
        # 没装 Pillow 也能用：退回内容摘要，代价是重压缩会被当成换了头像。
        return "m" + hashlib.md5(data).hexdigest()[:16]


def distance(fp_a: str | None, fp_b: str | None) -> int:
    """两个指纹差多少 bit；类型不同或缺失就算作完全不同。"""
    if not fp_a or not fp_b or fp_a[0] != fp_b[0]:
        return 1 << 30
    try:
        a, b = int(fp_a[1:], 16), int(fp_b[1:], 16)
    except ValueError:
        return 1 << 30
    return bin(a ^ b).count("1")


class AvatarStore:
    """按 QQ 缓存头像和指纹，落在 plugin_data 下的 avatars/ 里。"""

    def __init__(self, base_dir: str | Path, refresh_seconds: int = REFRESH_SECONDS) -> None:
        self.base = Path(base_dir) / AVATAR_DIR
        self.refresh_seconds = refresh_seconds

    # ------------------------------------------------------------ 索引读写

    @property
    def index_path(self) -> Path:
        return self.base / INDEX_NAME

    def _load_index(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _save_index(self, index: dict[str, dict[str, Any]]) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.index_path)

    def image_path(self, qq: str | int) -> Path:
        return self.base / f"{str(qq).strip()}.jpg"

    @staticmethod
    def _needs_desc(entry: dict[str, Any], first_seen: bool, changed: bool) -> bool:
        """这张脸还欠一次描述吗。

        第一次见、换了头像，或者上次描述没成、补考次数还没用完，都算欠。
        之前只有「第一次见 / 换了头像」会触发描述，网络抖一下这张脸就永远没描述了，
        MAX_DESC_TRIES 就是给这种时候留的后路。
        """
        if entry.get("desc"):
            return False
        if first_seen or changed:
            return True
        return int(entry.get("desc_tries") or 0) < MAX_DESC_TRIES

    # ------------------------------------------------------------ 核心快照

    def snapshot(
        self,
        qq: str | int,
        *,
        force: bool = False,
        size: int = DEFAULT_SIZE,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """拿到某个 QQ 的头像现状。

        返回 dict：
            qq / path / fp / changed / first_seen / fresh / desc / at / error
        changed 为 True 表示和上次记的不一样（换头像了）；
        fresh 为 True 表示这次真的重新下载过。
        """
        qq = str(qq).strip()
        index = self._load_index()
        entry = dict(index.get(qq) or {})
        path = self.image_path(qq)
        now = time.time()

        recent = now - float(entry.get("at") or 0) < self.refresh_seconds
        if not force and recent and path.exists():
            return {
                "qq": qq,
                "path": str(path),
                "fp": entry.get("fp"),
                "changed": False,
                "first_seen": False,
                "fresh": False,
                "desc": entry.get("desc") or "",
                "needs_desc": self._needs_desc(entry, False, False),
                "at": entry.get("at"),
                "error": "",
            }

        try:
            data, url = fetch_avatar(qq, size=size, timeout=timeout)
        except Exception as exc:
            result = {
                "qq": qq,
                "path": str(path),
                "fp": entry.get("fp"),
                "changed": False,
                "first_seen": False,
                "fresh": False,
                "desc": entry.get("desc") or "",
                "needs_desc": False,
                "at": entry.get("at"),
                "error": str(exc),
            }
            return result

        fp = fingerprint(data)
        old_fp = entry.get("fp")
        drift = distance(old_fp, fp)
        first_seen = not old_fp
        changed = bool(old_fp) and drift > MAX_DRIFT

        self.base.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

        entry.update(
            {
                "fp": fp,
                "at": now,
                "url": url,
                "bytes": len(data),
                "drift": drift,
            }
        )
        # 换了头像，旧的描述就作废，等重新描述。
        if changed or first_seen:
            entry.pop("desc", None)
        index[qq] = entry
        self._save_index(index)

        return {
            "qq": qq,
            "path": str(path),
            "fp": fp,
            "changed": changed,
            "first_seen": first_seen,
            "fresh": True,
            "desc": entry.get("desc") or "",
            "needs_desc": self._needs_desc(entry, first_seen, changed),
            "at": now,
            "bytes": len(data),
            "drift": drift,
            "error": "",
        }

    # ------------------------------------------------------------ 描述缓存

    def set_description(self, qq: str | int, desc: str) -> None:
        qq = str(qq).strip()
        index = self._load_index()
        entry = dict(index.get(qq) or {})
        entry["desc"] = (desc or "").strip()[:400]
        entry["desc_at"] = time.time()
        entry.pop("desc_tries", None)
        index[qq] = entry
        self._save_index(index)

    def mark_desc_attempt(self, qq: str | int) -> int:
        """记一次描述失败，返回累计失败次数。"""
        qq = str(qq).strip()
        index = self._load_index()
        entry = dict(index.get(qq) or {})
        entry["desc_tries"] = int(entry.get("desc_tries") or 0) + 1
        index[qq] = entry
        self._save_index(index)
        return entry["desc_tries"]

    def get(self, qq: str | int) -> dict[str, Any]:
        return dict(self._load_index().get(str(qq).strip()) or {})

    def forget(self, qq: str | int) -> bool:
        """删掉某个 QQ 的头像记录与文件。"""
        qq = str(qq).strip()
        index = self._load_index()
        existed = qq in index
        index.pop(qq, None)
        self._save_index(index)
        try:
            self.image_path(qq).unlink()
        except Exception:
            pass
        return existed


def note_line(snap: dict[str, Any], who: str = "对方") -> str:
    """把快照压成一行给系统提示词用的话。

    只在真有新消息的时候出声：第一次见，或者他换了脸。
    指纹没变说明这张脸的描述上一轮已经给过了，
    再塞一遍纯粹是占地方。
    """
    if snap.get("error"):
        return ""
    if not (snap.get("first_seen") or snap.get("changed")):
        return ""
    parts = [f"第一次拿到{who}的头像" if snap.get("first_seen") else f"{who}换了头像"]
    desc = snap.get("desc")
    if desc:
        parts.append(f"现在这张是：{desc}")
    else:
        parts.append("还没看清这张画的是什么")
    return "【头像线索】" + "；".join(parts) + "。只当背景，不要主动报备截图。"
