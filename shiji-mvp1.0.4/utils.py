"""Rule-based extraction, LLM fallback, and public extract_info API."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ai_module.config import (
    CANTEEN_ALIAS,
    CANTEENS,
    DISH_SUFFIXES,
    EMOTION_KEYWORDS,
    LLM_CONFIG,
    SYSTEM_PROMPT,
)

_HAO = "号"
_CH_DIGITS = "一二三四五六七八九十"
_LOU = "楼"
_CHUANG = "窗口"
_DANG = "档口"
_TAN = "摊位"

_SHOP_PATTERN = re.compile(
    rf"([\d{_CH_DIGITS}]+)({_HAO})?({_CHUANG}|{_DANG}|{_TAN})"
)
_FLOOR_PATTERN = re.compile(rf"[{_CH_DIGITS}\d]+{_LOU}")
_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")


def _match_canteen(text: str) -> str | None:
    """Locate canteen (full or alias) and optional floor after it."""
    chosen: str | None = None
    pos = -1
    for name in CANTEENS:
        i = text.find(name)
        if i != -1 and (pos == -1 or i < pos):
            chosen, pos = name, i
    if chosen is None:
        for alias, full in CANTEEN_ALIAS.items():
            i = text.find(alias)
            if i != -1 and (pos == -1 or i < pos):
                chosen, pos = full, i
    if chosen is None:
        return None
    after = text[pos + len(chosen) :]
    floor = _FLOOR_PATTERN.search(after)
    if floor:
        return chosen + floor.group(0)
    return chosen


def _match_shop(text: str) -> str | None:
    """Match window/stall phrase; normalize to X号窗口."""
    m = _SHOP_PATTERN.search(text)
    if not m:
        return None
    num = m.group(1)
    return f"{num}{_HAO}{_CHUANG}"


def _match_dish(text: str) -> str | None:
    """Earliest dish suffix; stem is up to 6 chars before suffix (after last de if any)."""
    best_pos: int | None = None
    best_suff: str | None = None
    for suff in DISH_SUFFIXES:
        idx = text.find(suff)
        if idx == -1:
            continue
        if best_pos is None or idx < best_pos:
            best_pos, best_suff = idx, suff
    if best_pos is None or best_suff is None:
        return None
    prefix = text[:best_pos]
    _de = "的"
    _mai = "卖"
    tail = prefix.rsplit(_de, 1)[-1]
    if _mai in tail:
        tail = tail.rsplit(_mai, 1)[-1]
    m = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{1,6})$", tail)
    if not m:
        return None
    stem = m.group(1)
    return stem + best_suff


def _extract_quote(text: str) -> str:
    """First 30 chars or full text; remove matched canteen and shop once."""
    canteen = _match_canteen(text)
    shop = _match_shop(text)
    frag = text if len(text) <= 30 else text[:30]
    out = frag
    if canteen:
        out = out.replace(canteen, "", 1)
    if shop:
        out = out.replace(shop, "", 1)
    out = out.strip()
    if not out:
        out = frag[:30].strip()
    if len(out) > 30:
        out = out[:30]
    return out


def _extract_tags(text: str) -> list[str]:
    """Tag hits from EMOTION_KEYWORDS; stable unique order."""
    seen: set[str] = set()
    tags: list[str] = []
    for label, keywords in EMOTION_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                if label not in seen:
                    seen.add(label)
                    tags.append(label)
                break
    return tags


def normalize_emotion_tags(tags: list[str] | None) -> list[str]:
    """只保留预设情绪池中的标签，顺序去重，最多 5 个。"""
    if not tags:
        return []
    allowed = set(EMOTION_KEYWORDS.keys())
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        if not isinstance(t, str):
            continue
        s = t.strip()
        if s in allowed and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= 5:
            break
    return out


def _parse_llm_json_content(raw: str) -> dict[str, Any] | None:
    """Parse JSON from model text; support fenced code blocks."""
    s = raw.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", s)
    if fence:
        s = fence.group(1).strip()
    try:
        data = json.loads(s)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _normalize_llm_result(data: dict[str, Any]) -> dict[str, Any] | None:
    """Require non-empty canteen; coerce quote/tags."""
    canteen = data.get("canteen")
    if not isinstance(canteen, str) or not canteen.strip():
        return None
    shop = data.get("shop_name")
    dish = data.get("dish_name")
    quote = data.get("quote")
    tags_raw = data.get("tags")
    if not isinstance(quote, str):
        quote = ""
    quote = quote[:30]
    tags: list[str] = []
    if isinstance(tags_raw, list):
        for t in tags_raw:
            if isinstance(t, str) and t.strip():
                tags.append(t.strip())
    tags = normalize_emotion_tags(tags)
    return {
        "canteen": canteen.strip(),
        "shop_name": shop if isinstance(shop, str) and shop.strip() else None,
        "dish_name": dish if isinstance(dish, str) and dish.strip() else None,
        "quote": quote,
        "tags": tags,
    }


def _call_llm(text: str) -> dict | None:
    """Zhipu OpenAI-compatible chat; None on timeout, JSON, or API errors."""
    key = os.environ.get(LLM_CONFIG["api_key_env"])
    if not key:
        return None

    try:
        import httpx
    except Exception:
        httpx = None  # type: ignore[assignment]

    try:
        from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError
    except Exception:
        return None

    optional_status: tuple[type[BaseException], ...] = ()
    try:
        from openai import APIStatusError

        optional_status = (APIStatusError,)
    except Exception:
        pass

    try:
        client = OpenAI(
            api_key=key,
            base_url=LLM_CONFIG["base_url"],
            timeout=LLM_CONFIG["timeout"],
        )
        resp = client.chat.completions.create(
            model=LLM_CONFIG["model"],
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )
        choice = resp.choices[0].message.content
        if choice is None:
            return None
        parsed = _parse_llm_json_content(choice)
        if parsed is None:
            return None
        return _normalize_llm_result(parsed)
    except TimeoutError:
        return None
    except APITimeoutError:
        return None
    except (APIError, APIConnectionError, RateLimitError, *optional_status):
        return None
    except Exception as e:
        if httpx is not None and isinstance(
            e,
            (
                httpx.ReadTimeout,
                httpx.ConnectTimeout,
                httpx.TimeoutException,
            ),
        ):
            return None
        return None


def extract_info(text: str | None) -> dict | None:
    """Rules first; LLM only when canteen missing; None if still no canteen."""
    if text is None or not str(text).strip():
        return None
    text = str(text)

    canteen = _match_canteen(text)
    shop_name = _match_shop(text)
    dish_name = _match_dish(text)
    quote = _extract_quote(text)
    tags = _extract_tags(text)

    if canteen:
        if len(quote) > 30:
            quote = quote[:30]
        tags = normalize_emotion_tags(tags)
        return {
            "canteen": canteen,
            "shop_name": shop_name,
            "dish_name": dish_name,
            "quote": quote,
            "tags": tags,
        }

    llm = _call_llm(text)
    if llm is not None:
        llm["tags"] = normalize_emotion_tags(llm.get("tags"))
        return llm
    return None
