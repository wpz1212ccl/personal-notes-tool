"""v6 二期（规格 §Implementation·二期的预设标签，票 #85）：预设标签。

- **规则文件** `data/preset-tags.json`（与 `hotkey-status.json` 同目录、同 JSON 范式，
  **不放进 `config.ini`**——规则表塞进单行键值会立刻需要转义约定）。文件缺失 / 坏掉 /
  结构不对一律**回落内置默认**，不抛异常（照 `settings.read_hotkey_status` 的读法）。
- **派生与匹配**（`preset_tags_for`）是**纯函数**：出处派生（站点 / 账号 / 分类，按
  `provenance` 三个开关）+ 关键词规则（同时看会话标题与词条原句）。只做字符串匹配，
  **零网络**、零数据库 —— 所以它能直接进捕获关键路径。
- **标签名清洗**（`clean_tag`）：去 `#` 前缀 → 空白归一为 `-` → 截断。`records.tags`
  是空格分隔字符串，含空格的标签（`Bob the Canadian`）会被切碎，必须先归一成
  `Bob-the-Canadian`。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger("hub.preset_tags")

PRESET_TAGS_NAME = "preset-tags.json"  # 默认与数据库同目录（db parent）
TAG_MAX = 32  # 标签名限长（超出截断；标签是空格分隔格式里的短词）
MAX_RULES = 200  # 规则条数上限（坏文件写出巨型表也不许拖慢捕获）
PROVENANCE_KEYS = ("site", "account", "category")
_WHITESPACE = re.compile(r"\s+")
_ASCII_ALNUM = re.compile(r"[A-Za-z0-9]")  # 词边界的相邻字符类（票 #92）

# 内置默认（文件缺失 / 坏掉时用它）。规则表就是规格里那份示例。
DEFAULT_PRESET_TAGS: dict[str, Any] = {
    "provenance": {"site": True, "account": True, "category": False},
    "rules": [
        {"tag": "外教英语", "match": ["外教英语"]},
        {"tag": "TED", "match": ["TED演讲", "TED-Ed", "TED"]},
        {"tag": "雅思", "match": ["雅思", "IELTS"]},
    ],
}


def clean_tag(value: Any) -> str:
    """标签名单词清洗：去 `#` 前缀 → 空白归一为 `-` → 截断到 `TAG_MAX`；空 → 空串。"""
    text = "" if value is None else str(value)
    text = text.strip().lstrip("#").strip()
    return _WHITESPACE.sub("-", text)[:TAG_MAX]


def _defaults() -> dict[str, Any]:
    """内置默认的一份深拷贝（调用方改它不许污染模块常量）。"""
    return json.loads(json.dumps(DEFAULT_PRESET_TAGS))


def _flag(value: Any, default: bool) -> bool:
    """`provenance` 开关归一：缺键回默认；手编文件里的 `"false"` 也算 false。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def normalize_preset_tags(payload: Mapping[str, Any]) -> dict[str, Any]:
    """结构归一：`provenance` 三个 bool + `rules` 里 `tag`/`match` 都清成非空字符串表。

    用不了的规则（标签清完是空、一条匹配词都没有）直接丢掉 —— 规则表是数据库之外的第二
    真源，宁可少一条规则也不许把坏标签写进 `records.tags`。
    """
    raw_flags = payload.get("provenance")
    flags = raw_flags if isinstance(raw_flags, Mapping) else {}
    provenance = {
        key: _flag(flags.get(key), bool(DEFAULT_PRESET_TAGS["provenance"][key]))
        for key in PROVENANCE_KEYS
    }
    rules: list[dict[str, Any]] = []
    raw_rules = payload.get("rules")
    for item in raw_rules if isinstance(raw_rules, list) else []:
        if not isinstance(item, Mapping):
            continue
        tag = clean_tag(item.get("tag"))
        raw_match = item.get("match")
        if isinstance(raw_match, str):
            raw_match = [raw_match]
        terms = (
            [str(term).strip() for term in raw_match]
            if isinstance(raw_match, Sequence)
            else []
        )
        terms = [term for term in terms if term]
        if not tag or not terms:
            continue
        rules.append({"tag": tag, "match": terms})
        if len(rules) >= MAX_RULES:
            log.warning("preset-tags.json 规则超过 %d 条，多余的忽略", MAX_RULES)
            break
    return {"provenance": provenance, "rules": rules}


def read_preset_tags(path: Path | str) -> dict[str, Any]:
    """读规则文件；文件不在 / 坏掉 / 顶层不是对象 → **内置默认**（不抛异常）。"""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        log.debug("preset-tags.json 不存在，用内置默认规则")
        return _defaults()
    except (OSError, UnicodeDecodeError):
        log.warning("preset-tags.json 读不出来，用内置默认规则")
        return _defaults()
    try:
        payload = json.loads(raw)
    except ValueError:
        log.warning("preset-tags.json 不是合法 JSON，用内置默认规则")
        return _defaults()
    if not isinstance(payload, Mapping):
        log.warning("preset-tags.json 顶层不是对象，用内置默认规则")
        return _defaults()
    return normalize_preset_tags(payload)


def write_preset_tags(path: Path | str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """写规则文件（原子替换：同目录 `.tmp` + `os.replace`，照 `media-pause.json` 写法）。

    写前先归一，所以落盘的永远是干净结构；返回写下的内容。
    """
    target = Path(path)
    data = normalize_preset_tags(payload)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temp, target)
    return data


def _term_matches(term: str, haystack: str) -> bool:
    """单条匹配词是否命中 `haystack`（已小写）——票 #92 的词边界规则。

    - 词条两端是 **ASCII 字母 / 数字**时补负向环视（前后不能是字母数字）：
      裸词 `ted` 于是不再命中 `exhausted` / `wanted` / `started` 这类含 `ted` 的英文词；
    - **CJK 一侧不补**（中文没有词边界）：`外教英语` 仍可夹在中文句子里命中，
      中英混排的 `TED演讲` 也只对 ASCII 那一侧要边界。
    """
    if not term:
        return False
    prefix = r"(?<![A-Za-z0-9])" if _ASCII_ALNUM.match(term[0]) else ""
    suffix = r"(?![A-Za-z0-9])" if _ASCII_ALNUM.match(term[-1]) else ""
    return re.search(prefix + re.escape(term.lower()) + suffix, haystack) is not None


def preset_tags_for(
    config: Mapping[str, Any],
    *,
    site: Any = "",
    account: Any = "",
    category: Any = "",
    title: Any = "",
    context: Any = "",
) -> list[str]:
    """算出一条记录的预设标签（已清洗、去重、保序）；出处在前，规则命中在后。

    `config` 是 `read_preset_tags` 的产物。规则匹配**同时看会话标题与词条原句**，
    大小写不敏感（`IELTS` / `ielts` 都算）。ASCII 词条按**词边界**匹配、CJK 词条按子串
    （`_term_matches`，票 #92）——裸词 `TED` 不再命中 `exhausted` / `wanted`。
    """
    provenance = config.get("provenance")
    flags = provenance if isinstance(provenance, Mapping) else {}
    picked: list[str] = []
    for key, value in (("site", site), ("account", account), ("category", category)):
        if not flags.get(key):
            continue
        tag = clean_tag(value)
        if tag and tag not in picked:
            picked.append(tag)
    haystack = f"{title}\n{context}".lower()
    raw_rules = config.get("rules")
    for rule in raw_rules if isinstance(raw_rules, Sequence) else []:
        if not isinstance(rule, Mapping):
            continue
        tag = clean_tag(rule.get("tag"))
        if not tag or tag in picked:
            continue
        terms = rule.get("match")
        if isinstance(terms, str):
            terms = [terms]
        if any(_term_matches(str(term), haystack) for term in (terms or [])):
            picked.append(tag)
    return picked
