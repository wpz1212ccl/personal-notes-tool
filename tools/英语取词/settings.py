"""设置面板的读写（票 #32；规格 §8.4 §9.1）。

两件事：

1. **`config.ini` 的写回**——`update_config_text` 做**行级编辑**：只动 `[英语取词]`
   段里目标键的那一行，注释、键顺序、其它段、行尾 CRLF 全部原样保留；段里没有的键
   追加到段尾。`atomic_write` 先写同目录临时文件再 `os.replace`，写失败原文件不动
   （AC：保留注释 + 原子写）。校验在动文件之前做完，非法值一律不落盘。
2. **`data/hotkey-status.json` 的读取**——只读。探测是外壳（AHK）的事（票 #28），
   hub 没有反向通道，所以这里不探测、不写它，只把外壳写的结果交给前端。

本模块不 log 用户内容：键位字符串是配置不是用户内容，但出错只记键名与原因（§10）。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from sessions import DEFAULT_IDLE_MIN
from speech import DEFAULT_VOICE, VOICES

log = logging.getLogger("hub")

SECTION = "英语取词"

# §8.4 的动作表（顺序 = 设置页的行顺序）。键名与 `config.ini` / 外壳同名，
# 也就是 `hotkey-status.json` 里 `items[].action` 的取值（票 #28 的契约）。
# scope：global = 系统全局键（外壳 RegisterHotKey）；box = 速记框内键。
# v34 T2（#215）：补齐长文两键（一直在注册却看不见）与 v34 新增两键
# （字幕带拖框 / 开始·停止收句）——「有个键在生效但我不知道」从这个表上消失。
HOTKEY_ACTIONS: tuple[tuple[str, str, str], ...] = (
    ("hotkey", "呼出速记框", "global"),
    ("hotkey_capture", "直接截图取词", "global"),
    ("hotkey_review", "打开复习界面", "global"),
    ("hotkey_new_session", "强制新会话", "global"),
    ("hotkey_longform_ocr", "长文整页识别", "global"),
    ("hotkey_longform_paste", "长文粘贴成文", "global"),
    ("hotkey_capture_arm", "开始/停止收句", "global"),
    ("hotkey_redrag", "字幕带拖框", "global"),
    ("key_save", "保存", "box"),
    ("key_cancel", "取消", "box"),
    ("key_shot", "框内截图", "box"),
    ("key_more", "展开元数据", "box"),
    ("key_context", "展开原句", "box"),
    ("key_help", "快捷键说明", "box"),
    ("key_lookup", "重查词典", "box"),
)

# §9.1 的默认值（config.ini 缺键时前端也要显示得出来）。
# 默认值必须互不重复：外壳按默认值注册，两个动作同键就是「按了没反应」（v34 T2）。
HOTKEY_DEFAULTS: dict[str, str] = {
    "hotkey": "^!1",
    "hotkey_capture": "^!2",
    "hotkey_review": "^!3",
    "hotkey_new_session": "",
    "hotkey_longform_ocr": "^!4",
    "hotkey_longform_paste": "^!5",
    "hotkey_capture_arm": "^!7",
    "hotkey_redrag": "^!6",
    "key_save": "^Enter",
    "key_cancel": "Escape",
    "key_shot": "^s",
    "key_more": "F2",
    "key_context": "F3",
    "key_help": "F1",
    "key_lookup": "^l",
}

HOTKEY_KEYS: tuple[str, ...] = tuple(action for action, _, _ in HOTKEY_ACTIONS)
SCOPE_BY_KEY: dict[str, str] = {action: scope for action, _, scope in HOTKEY_ACTIONS}
LABEL_BY_KEY: dict[str, str] = {action: label for action, label, _ in HOTKEY_ACTIONS}

THEMES = ("dark", "light")
IDLE_MIN_RANGE = (1, 1440)  # 1 分钟 ~ 24 小时，比这更离谱的输入一定是打错了

# §9.2 的基础键表：修饰符 + 这些名字（大小写不敏感）之外，只允许单个字母/数字
# （框内键另外允许单标点，见 _BOX_PUNCT）。
_NAMED_BASES = (
    {f"f{index}" for index in range(1, 25)}
    | {
        "escape",
        "enter",
        "space",
        "tab",
        "backspace",
        "del",
        "delete",
        "insert",
        "ins",
        "home",
        "end",
        "pgup",
        "pageup",
        "pgdn",
        "pagedown",
        "up",
        "down",
        "left",
        "right",
    }
    | {f"numpad{index}" for index in range(10)}
    | {"numpadadd", "numpadsub", "numpadmult", "numpaddiv", "numpaddot"}
)
_MODIFIER_ORDER = "^!+#"
# 票 #48（B2）：`~` 放行前缀（AHK 的 pass-through）与修饰符同处前缀位，但**不是**
# 修饰符 → 不能重复、也不计入「有没有修饰键」；外壳的 ParseHotkeyV2 收同一写法。
_PASSTHROUGH = "~"
# 真·不能进 config.ini 的字符：`;` 是行内注释、`=` 是键值分隔、空白是行边界。
# 其它符号（[ ] " ' \ 等）留在值里没问题 —— 它们只有在框内单标点那条路才可能成为键。
_FORBIDDEN = set(" \t\r\n;=")
# §9.2：框内键允许的单标点（外壳走 VkKeyScanW 取 vk，见 Logic.ahk 的 ParseHotkeyV2）。
# 排除 ^!+#（AHK 修饰符）、{ }（AHK 转义）、; =（ini 语法）与空白。
_BOX_PUNCT = frozenset(",-./'`[]\\")

# v34 T2（#215）：判重用的规范化表 —— 与 Logic.ahk 的 NamedVk / CanonKey 同口径。
_MOD_BITS = {"^": "ctrl", "!": "alt", "+": "shift", "#": "win"}
_KEY_ALIASES = {
    "del": "delete",
    "ins": "insert",
    "pgup": "pageup",
    "pgdn": "pagedown",
}


def hotkey_error(spec: str, scope: str) -> str | None:
    """键位语法校验（§9.2 的客户端一侧）。空串 = 不绑定，合法；否则返回中文原因。"""
    text = str(spec or "").strip()
    if not text:
        return None
    if any(char in _FORBIDDEN for char in text):
        return "键位里不能有空格、分号或等号"
    index = 0
    modifiers = ""
    passthrough = False
    while index < len(text) and text[index] in _MODIFIER_ORDER + _PASSTHROUGH:
        char = text[index]
        if char == _PASSTHROUGH:
            if passthrough:
                return "放行前缀 ~ 只能写一次（如 ~!1）"
            passthrough = True
        elif char in modifiers:
            return f"修饰键 {char} 重复了"
        else:
            modifiers += char
        index += 1
    base = text[index:]
    if not base:
        # `~` 单独：放行前缀不是基础键，缺了基础键照样是语法错（外壳同一条口径）
        return "只有放行前缀 ~，没有基础键" if passthrough else "只有修饰键，没有基础键"
    # `~` 写在基础键之后（`!1~`）：外壳也拒这种串（前缀必须写在基础键之前）
    if _PASSTHROUGH in base:
        return "放行前缀 ~ 必须写在基础键之前（如 ~!1）"
    single = len(base) == 1 and (base.isalpha() or base.isdigit())
    # 单标点只对框内键开放（外壳的 allowSingleChar 也只对框内开）：
    # 全局单标点会抢走全系统的标点输入，且外壳本身就拒。
    punct = scope == "box" and len(base) == 1 and base in _BOX_PUNCT
    if not single and not punct and base.lower() not in _NAMED_BASES:
        return f"不认识的基础键「{base}」"
    # 用户要求 8：无修饰的字母/数字键不能当全局键（会吃掉全系统的输入）。
    # 框内键（scope=box）只在速记框显示时生效，单字符是合法的（§9.2）。
    if scope == "global" and not modifiers and (single or punct):
        return "全局键必须带 Ctrl / Alt / Shift / Win 里的至少一个修饰键（或改用 F1–F24 这类功能键）"
    return None


def hotkey_combo(spec: str, scope: str) -> tuple[str, str, frozenset[str]] | None:
    """规范化后的「物理组合」`(scope, 基础键, 修饰符集合)`；空串 / 语法非法 → `None`。

    判重口径与外壳 `HotkeyStatuses` 一致（`(group, mods, vk)`）：`~` 放行前缀不参与
    比较（`~!1` 与 `!1` 是同一个物理组合）、修饰符顺序无关、基础键别名归一
    （`Del`=`Delete`、`F1`=`f1`）。作用域进元组 = 组内判重、组间不判重（外壳同款）。
    """
    text = str(spec or "").strip()
    if not text or hotkey_error(text, scope):
        return None
    mods: set[str] = set()
    index = 0
    while index < len(text) and text[index] in _MODIFIER_ORDER + _PASSTHROUGH:
        bit = _MOD_BITS.get(text[index])
        if bit:
            mods.add(bit)
        index += 1
    base = text[index:]
    if len(base) == 1:
        canonical = base.upper() if base.isalpha() else base
    else:
        canonical = _KEY_ALIASES.get(base.lower(), base.lower())
    return (scope, canonical, frozenset(mods))


def _duplicate_hotkey_errors(
    effective: dict[str, str], touched: set[str]
) -> dict[str, str]:
    """同组内两个动作落在同一个物理组合 → 每个冲突动作一条可读理由。

    `effective` 是「保存后的有效键位」：config 现值叠加本次提交（见 `validate` 的
    `current`）。只有**这次提交里出现过的动作**参与 —— 老 config 里手写出来的重复
    不该拦住一次与热键无关的保存（只改主题 / 空闲阈值）。
    """
    groups: dict[tuple[str, str, frozenset[str]], list[str]] = {}
    for key in HOTKEY_KEYS:
        spec = str(effective.get(key) or "").strip()
        combo = hotkey_combo(spec, SCOPE_BY_KEY[key])
        if combo is not None:
            groups.setdefault(combo, []).append(key)
    errors: dict[str, str] = {}
    for keys in groups.values():
        if len(keys) < 2 or not touched.intersection(keys):
            continue
        spec = str(effective.get(keys[0]) or "").strip()
        names = "」「".join(LABEL_BY_KEY[key] for key in keys)
        message = (
            f"键位 {spec} 同时落在「{names}」上："
            "同一个键只能给一个动作（外壳会把两条都判重复、都不注册），改掉其中一个再保存"
        )
        for key in keys:
            errors[key] = message
    return errors


def config_int(config: dict[str, str], key: str, default: int) -> int:
    try:
        return int(_clean(config[key]))
    except (KeyError, ValueError):
        return default


def _clean(raw: str) -> str:
    """取行内注释（`; ...`）之前的真值。configparser 默认不剥行内注释，这里剥。

    只切 `;`：`#` 是 AHK 的 Win 修饰符（`#s`），不能当注释符。
    """
    return str(raw).split(";", 1)[0].strip()


def settings_view(config: dict[str, str]) -> dict[str, Any]:
    """读出来的设置（缺键回落 §9.1 默认值），给 `GET /api/settings`。"""
    theme = _clean(config.get("theme", "dark")).lower()
    low, high = IDLE_MIN_RANGE
    idle = config_int(config, "session_idle_min", DEFAULT_IDLE_MIN)
    dict_online = config_int(config, "dict_online", 1)
    # v5 §8：两个开关默认开（1）；`GET` 与 `PUT` 的回包都带上，设置页据此回显
    probe_url = config_int(config, "probe_url", 1)
    media_pause = config_int(config, "media_pause", 1)
    # v6 三期（票 #89）：账号解析联网开关，同样默认开
    resolve_account_online = config_int(config, "resolve_account_online", 1)
    # v11 §7（票 #112）：在线神经语音总开关，默认开（0 = 零 Edge 请求，直接本机 SAPI）
    tts_online = config_int(config, "tts_online", 1)
    # 票 #116：音色可写（设置页下拉）；非法值回落到内置默认，避免坏配置让发音不可用
    tts_voice = _clean(config.get("tts_voice", DEFAULT_VOICE))
    # v15 T2（票 #127）：阅读页纸面滚轮换段，默认开（0 = 只滚正文、不换段）
    wheel_nav = config_int(config, "wheel_nav", 1)
    # v33 T5（票 #211）：「视频收句」总开关，默认开（0 = 零查轨零采样，全链静默）
    video_capture = config_int(config, "video_capture", 1)
    return {
        "hotkeys": {
            key: _clean(config.get(key, HOTKEY_DEFAULTS[key])) for key in HOTKEY_KEYS
        },
        "dict_online": 1 if dict_online != 0 else 0,
        "session_idle_min": idle if low <= idle <= high else DEFAULT_IDLE_MIN,
        "theme": theme if theme in THEMES else "dark",
        "probe_url": 1 if probe_url != 0 else 0,
        "media_pause": 1 if media_pause != 0 else 0,
        "resolve_account_online": 1 if resolve_account_online != 0 else 0,
        "tts_online": 1 if tts_online != 0 else 0,
        "tts_voice": tts_voice if tts_voice in VOICES else DEFAULT_VOICE,
        "wheel_nav": 1 if wheel_nav != 0 else 0,
        "video_capture": 1 if video_capture != 0 else 0,
    }


def _switch(raw: Any) -> str | None:
    """开关键校验（`dict_online` / v5 §8 的 `probe_url`、`media_pause` 共用）。

    真布尔（`false`→`"0"`、`true`→`"1"`）或字符串 `"0"`/`"1"` 可用，其余非法 → `None`。
    """
    if isinstance(raw, bool):
        return "1" if raw else "0"
    text = str(raw).strip()
    return text if text in ("0", "1") else None


def validate(
    patch: dict[str, Any], current: dict[str, str] | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """把 PUT 的 body 校验成 `{config 键: 将要写入的字符串}`；不合法项进 errors（键 → 原因）。

    `current` = 现在的 config 现值（`settings_view()["hotkeys"]`）。给它是为了跨动作
    重复校验能看见「这次没改的那个动作」：只改一个动作也可能撞上没动的那个。
    """
    updates: dict[str, str] = {}
    errors: dict[str, str] = {}

    hotkeys = patch.get("hotkeys")
    touched: set[str] = set()
    if hotkeys is not None:
        if not isinstance(hotkeys, dict):
            errors["hotkeys"] = "hotkeys 必须是对象"
        else:
            for key, raw in hotkeys.items():
                if key not in SCOPE_BY_KEY:
                    errors[str(key)] = "不是可改的键"
                    continue
                spec = str(raw or "").strip()
                scope = SCOPE_BY_KEY[key]
                # 空值 = 解绑（§9.1 的 hotkey_new_session 默认就是空），不算错
                reason = hotkey_error(spec, scope)
                if reason:
                    errors[key] = reason
                    continue
                updates[key] = spec
                touched.add(key)
            # v34 T2（#215）：跨动作重复校验。有效值 = 现值叠加本次提交，
            # 与外壳 HotkeyStatuses 的「组内判重」同口径（`~` 不算修饰符）。
            effective = dict(current or {})
            effective.update({key: updates[key] for key in touched})
            for key, message in _duplicate_hotkey_errors(effective, touched).items():
                errors.setdefault(key, message)

    if "session_idle_min" in patch:
        raw = patch["session_idle_min"]
        try:
            idle = int(str(raw).strip())
        except (TypeError, ValueError):
            idle = 0
        low, high = IDLE_MIN_RANGE
        if low <= idle <= high:
            updates["session_idle_min"] = str(idle)
        else:
            errors["session_idle_min"] = f"要在 {low}–{high} 分钟之间"

    if "theme" in patch:
        theme = str(patch["theme"] or "").strip().lower()
        if theme in THEMES:
            updates["theme"] = theme
        else:
            errors["theme"] = "只支持 dark / light"

    # 开关键统一走 `_switch`：真布尔（`false`→`0`）或 `"0"`/`"1"`。v5 §8 的
    # `probe_url` / `media_pause` 与既有 `dict_online` 是同一条路（票 #61：修掉
    # `dict_online=false` 被写成 `1` 的既有 bug）；v6 三期（票 #89）的
    # `resolve_account_online`、v11 §7（票 #112）的 `tts_online`、
    # v15 T2（票 #127）的 `wheel_nav`、v33 T5（票 #211）的 `video_capture`
    # 照抄同一条路。
    for key in (
        "dict_online",
        "probe_url",
        "media_pause",
        "resolve_account_online",
        "tts_online",
        "wheel_nav",
        "video_capture",
    ):
        if key in patch:
            normalized = _switch(patch[key])
            if normalized is None:
                errors[key] = "只能开或关"
            else:
                updates[key] = normalized

    # 票 #116：音色只接受 17 个 en-US 神经语音白名单里的值
    if "tts_voice" in patch:
        voice = str(patch["tts_voice"] or "").strip()
        if voice in VOICES:
            updates["tts_voice"] = voice
        else:
            errors["tts_voice"] = "不认识的音色（只支持 en-US 神经语音白名单）"

    return updates, errors


# --- config.ini：行级编辑 + 原子写 -------------------------------------------


def _split_eol(line: str) -> tuple[str, str]:
    body = line.rstrip("\r\n")
    return body, line[len(body) :]


def _key_of(body: str) -> str | None:
    """这一行是 `key=value` 的话返回小写键名；注释行 / 段头 / 空行返回 None。"""
    stripped = body.strip()
    if not stripped or stripped[0] in ";#[" or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip().lower() or None


def _replace_value(body: str, value: str) -> str:
    """只替换 `=` 后面的值，行尾注释（`; ...`）留着。"""
    index = body.index("=")
    tail = body[index + 1 :].lstrip()
    cut = tail.find(";")
    if cut < 0:
        new_body = body[: index + 1] + value
    else:
        core, comment = tail[:cut], tail[cut:]
        separator = core[len(core.rstrip()) :] or " "
        new_body = body[: index + 1] + value + separator + comment
    return new_body


def update_config_text(
    text: str, updates: dict[str, str], section: str = SECTION
) -> tuple[str, list[str]]:
    """返回 `(新文本, 真正改到的键)`。段不存在时抛 ValueError（绝不重排用户的文件）。"""
    if not updates:
        return text, []
    pending = {key.lower(): (key, value) for key, value in updates.items()}
    eol = "\r\n" if "\r\n" in text else "\n"
    out: list[str] = []
    applied: list[str] = []
    in_section = False
    seen_section = False

    def flush_missing() -> None:
        if not pending:
            return
        # 原文件最后一行没有换行符（手改过的 ini 常见）：先补一个，
        # 否则新追加的键会黏在上一行末尾（`key_save=^Enterkey_help=F1`）。
        if out and not out[-1].endswith(("\n", "\r")):
            out[-1] += eol
        for key, value in pending.values():
            out.append(f"{key}={value}{eol}")
            applied.append(key)

    for line in text.splitlines(keepends=True):
        body, line_eol = _split_eol(line)
        stripped = body.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section:
                flush_missing()
            in_section = stripped[1:-1].strip() == section
            if in_section:
                seen_section = True
            out.append(line)
            continue
        if in_section:
            key = _key_of(body)
            if key in pending:
                _, value = pending.pop(key)
                out.append(_replace_value(body, value) + line_eol)
                applied.append(key)
                continue
        out.append(line)
    if in_section:
        flush_missing()
    if not seen_section:
        raise ValueError(f"config.ini 里没有 [{section}] 段，拒绝写入")
    return "".join(out), applied


def atomic_write(path: Path, text: str) -> None:
    """同目录临时文件 + `os.replace`：失败原文件不动，也不留半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",  # 无 BOM（§9.1：UTF-8 无 BOM）
            newline="",  # 行尾自己控制，别让 Python 改写 CRLF
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        if temp is not None:
            temp.unlink(missing_ok=True)
        raise


def read_text_exact(path: Path) -> str:
    """按原样读（`newline=""`）：写回时绝不能把用户的 CRLF 悄悄改成 LF。"""
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def apply_config(config_path: Path, updates: dict[str, str]) -> list[str]:
    """读 → 改 → 原子写。任何一步失败都让异常冒出去（调用方回 400/500），文件保持原样。"""
    text = read_text_exact(Path(config_path))
    new_text, applied = update_config_text(text, updates)
    if not applied:
        return []
    atomic_write(Path(config_path), new_text)
    log.info("设置写回 config.ini：改了 %d 个键（%s）", len(applied), ",".join(applied))
    return applied


# --- hotkey-status.json：只读 -------------------------------------------------

# 动作键名 → 中文名（外壳写的 JSONL 不带 label，界面要给人看；键名与 config.ini 同名）
HOTKEY_LABELS: dict[str, str] = {
    "hotkey": "呼出速记框",
    "hotkey_capture": "直接截图取词",
    "hotkey_review": "打开取词台",
    "hotkey_new_session": "强制新会话",
    # v34 T2（#215）：四个全局动作进同一份中文名表（状态文件只有 action，没有 label）
    "hotkey_longform_ocr": "长文整页识别",
    "hotkey_longform_paste": "长文粘贴成文",
    "hotkey_capture_arm": "开始/停止收句",
    "hotkey_redrag": "字幕带拖框",
    "key_save": "保存",
    "key_cancel": "取消",
    "key_shot": "框内截图",
    "key_more": "展开元数据",
    "key_context": "展开原句",
    "key_help": "快捷键表",
    "key_lookup": "重查词典",
}


def read_hotkey_status(path: Path) -> dict[str, Any] | None:
    """外壳写的探测结果；文件不在 / 坏掉 / 结构不对都返回 None（前端显示「未探测」）。

    两种形态都收：外壳（票 #28 落地）写的是 **JSONL**（一行一条），而本模块原先只认单对象
    `{probed_at, items[]}`。两边都是我们自己写的，兼容成本比再统一一次接口低。
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    stripped = raw.strip()
    payload: Any = None
    if stripped.startswith("{"):
        try:  # 先按单对象试（JSONL 的第一行也是 `{...}`，解析成功即单对象）
            payload = json.loads(stripped)
        except ValueError:
            payload = None
    if payload is None:
        rows: list[Any] = []
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                log.warning("hotkey-status.json 里有一行不是合法 JSON，跳过")
        if not rows:
            log.warning("hotkey-status.json 读不出任何条目，按未探测处理")
            return None
        payload = rows

    if isinstance(payload, list):
        items, probed = payload, ""
    elif isinstance(payload, dict):
        raw_items = payload.get("items")
        items = raw_items if isinstance(raw_items, list) else []
        probed = str(payload.get("probed_at") or "")
    else:
        return None
    cleaned = [
        {
            "action": str(item.get("action") or ""),
            "label": str(
                item.get("label") or HOTKEY_LABELS.get(str(item.get("action") or "")) or ""
            ),
            "spec": str(item.get("spec") or ""),
            "state": str(item.get("state") or ""),
            "detail": str(item.get("detail") or ""),
        }
        for item in items
        if isinstance(item, dict)
    ]
    if isinstance(payload, list) and not probed:
        probed = next(
            (
                str(item.get("probed_at") or "")
                for item in payload
                if isinstance(item, dict)
            ),
            "",
        )
    return {"probed_at": probed, "items": cleaned}


# v34 T2（#215）：快照年龄的阈值。超过它就是「外壳可能没在运行」，不是「刚探测过」。
HOTKEY_STATUS_MAX_AGE_S = 300


def _probed_age_s(probed_at: Any, now: datetime | None) -> int | None:
    """`probed_at`（外壳本地时间 `yyyy-MM-dd HH:mm:ss`）距现在多少秒；读不出 → None。"""
    try:
        probed = datetime.strptime(str(probed_at or "").strip(), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return max(0, int(((now or datetime.now()) - probed).total_seconds()))


def hotkey_status_freshness(
    status: dict[str, Any] | None,
    hotkeys: dict[str, str],
    *,
    now: datetime | None = None,
    max_age_s: int = HOTKEY_STATUS_MAX_AGE_S,
) -> dict[str, Any]:
    """把「探测快照 vs 现在的 config」算成一句人话 + 一个可断言的枚举。

    外壳只在**重注册那一刻**写 `hotkey-status.json`（托盘暂停不写；别的程序后来抢键
    也不写），所以它天生是「探测那一刻的快照」。能从现有数据可靠判定的是两件事：

    1. **内容**：配置里绑了键的动作，快照报的 spec 与现值不一致（或压根没这一条）
       → 快照报的是旧键位，不能拿它的 `ok` 冒充新键位的结论（`stale_keys`）；
    2. **年龄**：`probed_at` 距现在超过 `max_age_s` → 外壳可能没在跑，旧快照只代表
       那一刻（`stale_age`，R2 §3.3 的降级提示）。

    不拿 config.ini 的 mtime 当判据：外壳拖框 / 切主题会自写 config（`box_x` 之类），
    mtime 变新而热键没动，会讹报「旧键位」。`state` ∈ missing / fresh / stale_keys / stale_age。
    """
    items = status.get("items") if isinstance(status, dict) else None
    if not items:
        # 没探测过不是「旧快照」：页面另有「未探测」文案，别混为一谈。
        return {"state": "missing", "stale": False, "mismatched": [], "age_s": None, "message": ""}
    reported = {str(item.get("action") or ""): str(item.get("spec") or "").strip() for item in items}
    mismatched: list[str] = []
    details: list[str] = []
    for key in HOTKEY_KEYS:
        spec = str(hotkeys.get(key) or "").strip()
        if not spec:
            continue  # 配置里就解绑了：外壳不注册也不报，不算快照的错
        old = reported.get(key, "")
        if old != spec:
            mismatched.append(key)
            details.append(f"{LABEL_BY_KEY[key]}（外壳报 {old or '没这条'}，现在是 {spec}）")
    age = _probed_age_s(status.get("probed_at"), now)
    if mismatched:
        return {
            "state": "stale_keys",
            "stale": True,
            "mismatched": mismatched,
            "age_s": age,
            "message": "外壳报的还是旧键位，等它重探（保存后约 2 秒）：" + "；".join(details),
        }
    if age is not None and age > max_age_s:
        minutes = max(1, age // 60)
        return {
            "state": "stale_age",
            "stale": True,
            "mismatched": [],
            "age_s": age,
            "message": (
                f"探测结果是 {minutes} 分钟前的旧快照，外壳可能没在运行；"
                "键位有没有被占用以重探为准"
            ),
        }
    return {"state": "fresh", "stale": False, "mismatched": [], "age_s": age, "message": ""}
