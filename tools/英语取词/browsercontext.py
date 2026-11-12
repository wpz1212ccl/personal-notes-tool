"""本机探针与媒体联动（v5 §2.2 / §3；票 #55 R1 + R2）。

**两个来源、两层结构**：

- **纯逻辑**（可单测、零 I/O）：地址栏启发式（`is_address_like` / `normalize_address` /
  `pick_address`）、应用 token（`app_token` / `same_app`）、标题宽松匹配（`loose_title_match`）、
  播放位置门（`position_seconds`）。判定口径逐字照 §2.2，最终 URL 仍走 `sessions.is_http_url`。
- **薄适配层**（惰性导入第三方库，任何异常都降级成 `None`）：
  - `read_address_bar`：UIA 读「本次捕获那只窗口」的地址栏（`comtypes` + `UIAutomationCore.dll`），
    在**工作线程 + 截止时间**里跑，超时/异常 → `None` 并冷却 30s；
  - `media_snapshot`：Windows SMTC（`winrt` 的
    `GlobalSystemMediaTransportControlsSessionManager`），**不发按键**（项目红线，§10），
    同样在**工作线程 + 截止时间**里跑，超时/异常 → 空白快照并冷却 30s（与 UIA 冷却独立）；
  - `media_pause` / `media_resume`：SMTC 停播/续播（同一条门）；
  - `probe_url`：两者合体，产出 §2.1 的 `browser` 对象（`source="probe"`）。

**日志纪律（§2.2 / §10）**：只记成功/失败、耗时、长度 —— **不记 URL、不记页面标题**
（`logs/hub.log` 的边界测试钉着这条）。所有第三方库都在函数内部导入，缺了只是探针/联动不可用。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import Any

from sessions import (
    format_timestamp,
    is_http_url,
    is_video_page,
    looks_like_video_title,
    site_of,
)

log = logging.getLogger("hub")

# §2.2：探针只对浏览器窗口读地址栏；不扫全屏、不读别的应用。
BROWSER_WINDOW_CLASSES = ("Chrome_WidgetWin_1", "MozillaWindowClass")
UIA_TIMEOUT_MS = 800
UIA_COOLDOWN_S = 30.0  # 超时/异常后 30s 内不再调 UIA（UIA 会卡）
SMTC_TIMEOUT_MS = 800  # SMTC 读取同一套保护：工作线程 + 截止时间
SMTC_COOLDOWN_S = 30.0  # SMTC 超时/异常后 30s 内不再调（与 UIA 冷却相互独立）
# 票 #181 H4（审计 H4）：worker 超时「留下自灭」= 滞留一条线程。连续滞留达 STUCK_LIMIT
# 改进 STUCK_COOLDOWN_S 长冷却：COM 永久卡死时线程累积从「每轮短冷却 1 条」压到
# 「每轮长冷却 1 条」，冷却期内不进探针；任何一次 worker 收尾（成功/异常）即清零。
STUCK_LIMIT = 3
STUCK_COOLDOWN_S = 1800.0  # 长冷却 30min：卡死场景下探针本就无用，留足自愈窗口
TITLE_LOOSE_MIN = 6  # 宽松匹配：较短一侧至少 6 字

_NAME_MAX = 2048  # 地址栏值上限（防呆：捡到超长文本一定不是地址）

# 中文（含全角标点）出现即拒：地址栏里绝不会是中文（§2.2）。
_CJK = re.compile(
    r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
)
_SPACE = re.compile(r"\s")
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://")
# `about:` / `file:` / `javascript:` 这种没有 `//` 的伪协议；`:8080` 是端口不是协议。
_BARE_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):(?![0-9])")
_HOST_CHARS = re.compile(r"^[A-Za-z0-9._\-]+$")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})


# --- 纯逻辑：地址栏启发式（§2.2）----------------------------------------------


def _authority_host(text: str) -> str:
    """从「scheme 之后的整串」里取出 host（去端口、path/query/fragment）。

    authority 里出现 `@`（`user@host` 的邮箱/凭据形态）一律拒（§2.2 宁缺勿错）——
    页面输入框里的邮箱值不许被当成链接。
    """
    cut = len(text)
    for mark in "/?#":
        index = text.find(mark)
        if index != -1:
            cut = min(cut, index)
    authority = text[:cut]
    if "@" in authority:
        return ""
    if authority.startswith("["):  # IPv6 字面量 [::1]:port
        end = authority.find("]")
        return authority[1:end] if end != -1 else ""
    return authority.split(":", 1)[0]


def _host_ok(host: str) -> bool:
    """host 合法 = 只含主机字符，且「有点」或本身就是 localhost / 127.0.0.1。

    不较真 `..` 与 `-` 边界——最终还有 `is_http_url` 兜底。
    """
    if not host or len(host) > _NAME_MAX or not _HOST_CHARS.match(host):
        return False
    low = host.lower()
    if low in _LOCAL_HOSTS:
        return True
    return "." in host and not host.startswith(".") and not host.endswith(".")


def is_address_like(value: Any) -> bool:
    """`s` 看起来像地址栏里的 URL？

    接受：可选 `http(s)://` + host（含点，或 `localhost`/`127.0.0.1`）+ 可选端口/path/query；
    拒绝：空、空白、中文、`edge://`/`chrome://`/`about:`/`file:`/`javascript:` 等非 http 协议、
    以及任何搜索词（含空格 / 无点的主机名）。
    """
    text = "" if value is None else str(value).strip()
    if not text or len(text) > _NAME_MAX:
        return False
    if _SPACE.search(text) or _CJK.search(text):
        return False
    match = _SCHEME.match(text)
    if match:
        if match.group(1).lower() not in ("http", "https"):
            return False
        rest = text[match.end() :]
    else:
        # 没有 `//` 的伪协议（`about:blank` / `javascript:...`）——但 `host:端口` 不算协议
        if _BARE_SCHEME.match(text):
            return False
        rest = text
    return _host_ok(_authority_host(rest))


def normalize_address(value: Any) -> str:
    """不像地址 → 空串；像地址 → 缺 scheme 补 `https://`，再过 `is_http_url`。"""
    text = "" if value is None else str(value).strip()
    if not is_address_like(text):
        return ""
    if not _SCHEME.match(text):
        text = "https://" + text
    return text if is_http_url(text) else ""


def pick_address(values: Iterable[Any]) -> str:
    """取第一个像地址的值（已归一）；都没有回空串。"""
    for value in values:
        normalized = normalize_address(value)
        if normalized:
            return normalized
    return ""


# --- 纯逻辑：应用 token / 标题 / 位置（§2.2 §3）--------------------------------


def app_token(app: Any) -> str:
    """捕获应用名 → SMTC 应用 id 的「同类」token（大小写无关子串比较用）。

    `Edge`→`MSEdge`、`Chrome`→`Chrome`、`Firefox`→`Firefox`（含 `Microsoft Edge` 这类写法）；
    其它应用按名字 casefold 尽力匹配。
    """
    name = "" if app is None else str(app).strip()
    low = name.casefold()
    if not low:
        return ""
    for hint, token in (("edge", "MSEdge"), ("chrome", "Chrome"), ("firefox", "Firefox")):
        if hint in low:
            return token
    return low


def same_app(app: Any, smtc_app_id: Any) -> bool:
    """SMTC 会话的 app id 与本次捕获的应用是不是同类（token 子串包含）。"""
    token = app_token(app)
    if not token:
        return False
    return token.casefold() in str(smtc_app_id or "").casefold()


def loose_title_match(smtc_title: Any, window_title: Any) -> bool:
    """宽松标题匹配（§2.2）：去空白、大小写无关、互为子串、较短一侧 ≥6 字。"""
    one = _SPACE.sub("", str(smtc_title or "")).casefold()
    two = _SPACE.sub("", str(window_title or "")).casefold()
    if not one or not two:
        return False
    shorter, longer = (one, two) if len(one) <= len(two) else (two, one)
    return len(shorter) >= TITLE_LOOSE_MIN and shorter in longer


def _to_seconds(value: Any) -> float | None:
    """timedelta / 数字 / 数字串 → 秒；其余 `None`。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, timedelta):
        return value.total_seconds()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def position_seconds(timeline: Mapping[str, Any] | None) -> float | None:
    """播放位置（秒）：门 = `end_time > 0` 且 `position` 在场（§2.2）。"""
    if not isinstance(timeline, Mapping):
        return None
    end = _to_seconds(timeline.get("end_time"))
    if end is None or end <= 0:
        return None
    return _to_seconds(timeline.get("position"))


# --- 适配层：UIA 读地址栏（§2.2）---------------------------------------------

_uia_lock = threading.Lock()
_uia_cooldown_until = 0.0
_uia_stuck = 0  # 连续滞留（超时「留下自灭」）计数（票 #181 H4）


def _uia_cooling() -> bool:
    with _uia_lock:
        return time.monotonic() < _uia_cooldown_until


def _start_uia_cooldown(reason: str, stuck: bool = False) -> None:
    """进入冷却；同一段冷却期只记一次日志（不记 URL）。

    票 #181 H4：worker 超时滞留时 `stuck=True` —— 连续滞留达 `STUCK_LIMIT` 改用
    `STUCK_COOLDOWN_S` 长冷却（原先是「一调一线程」超时后线程自灭、冷却一到期又
    再起一条，COM 永久卡死时线程随运行时长线性累积）。
    """
    global _uia_cooldown_until, _uia_stuck
    now = time.monotonic()
    with _uia_lock:
        if stuck:
            _uia_stuck += 1
        first = now >= _uia_cooldown_until
        span = STUCK_COOLDOWN_S if _uia_stuck >= STUCK_LIMIT else UIA_COOLDOWN_S
        _uia_cooldown_until = now + span
    if first:
        log.info("UIA 探针冷却：%s（%.0fs 内不再读地址栏）", reason, span)


def _uia_stuck_clear() -> None:
    """worker 收尾（成功/异常都算线程已退出，没滞留）：滞留计数清零（票 #181 H4）。"""
    global _uia_stuck
    with _uia_lock:
        _uia_stuck = 0


def _window_class(hwnd: int) -> str:
    """`GetClassNameW`（本机实测：Edge/Chrome = `Chrome_WidgetWin_1`，Firefox = `MozillaWindowClass`）。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    fn = user32.GetClassNameW
    fn.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    fn.restype = ctypes.c_int
    buffer = ctypes.create_unicode_buffer(256)
    if fn(wintypes.HWND(hwnd), buffer, 256) <= 0:
        return ""
    return buffer.value


def _element_value(element: Any) -> str:
    """读一个 UIA 元素的 ValuePattern 值（ValuePattern 的值不是 property，必须取 pattern）。"""
    try:
        import ctypes

        import comtypes
        from comtypes.gen import UIAutomationClient as UIA

        pointer = element.GetCurrentPatternAs(
            UIA.UIA_ValuePatternId, UIA.IUIAutomationValuePattern._iid_
        )
        if not pointer:
            return ""
        pattern = comtypes.cast(pointer, ctypes.POINTER(UIA.IUIAutomationValuePattern))
        return str(pattern.CurrentValue or "")
    except Exception:  # noqa: BLE001 - 单个元素读不到就跳过，绝不冒泡
        return ""


def _read_address_bar_now(hwnd: int) -> str | None:
    """UIA 本体（在 worker 线程里跑）：找带 ValuePattern 的 Edit 后代，取第一个像地址的值。"""
    if _window_class(hwnd) not in BROWSER_WINDOW_CLASSES:
        return None
    if _uia_cooling():  # 双保险：worker 里再判一次
        return None
    import comtypes.client

    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as UIA

    automation = comtypes.client.CreateObject(UIA.CUIAutomation)
    root = automation.ElementFromHandle(hwnd)
    condition = automation.CreatePropertyCondition(
        UIA.UIA_ControlTypePropertyId, UIA.UIA_EditControlTypeId
    )
    elements = root.FindAll(UIA.TreeScope_Descendants, condition)
    values: list[str] = []
    for index in range(elements.Length):
        value = _element_value(elements.GetElement(index))
        if value:
            values.append(value)
    return pick_address(values) or None


def read_address_bar(hwnd: int, timeout_ms: int = UIA_TIMEOUT_MS) -> str | None:
    """读某只窗口的地址栏；截止时间内没回来就 `None` + 冷却 30s（§2.2 健壮性）。

    `hwnd` 为 0/非法 → `None`。日志只记成败、耗时、长度。
    """
    try:
        handle = int(hwnd)
    except (TypeError, ValueError):
        return None
    if handle == 0 or _uia_cooling():
        return None
    started = time.perf_counter()
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = _read_address_bar_now(handle)
        except BaseException as exc:  # noqa: BLE001 - 任何异常都降级
            box["error"] = type(exc).__name__

    worker = threading.Thread(target=work, name="uia-address", daemon=True)
    worker.start()
    worker.join(max(0.0, timeout_ms / 1000.0))
    if worker.is_alive():
        # 票 #181 H4：超时「留下自灭」= 滞留一条，计入连续滞留（达阈值这次进长冷却）
        _start_uia_cooldown(f"超时 {timeout_ms}ms", stuck=True)
        return None
    _uia_stuck_clear()  # 线程收尾（成功/异常都算）：没滞留（票 #181 H4）
    if "error" in box:
        _start_uia_cooldown(f"异常 {box['error']}")
        return None
    value = box.get("value")
    if not isinstance(value, str) or not value:
        return None
    log.info("UIA 读地址栏成功（%dms，%d 字）", _ms(started), len(value))
    return value


# --- 适配层：SMTC 媒体会话（§2.2 读位置 + §3 停播/续播）------------------------

_PLAYBACK_NAMES = {4: "playing", 5: "paused"}


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _manager_class() -> Any:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager,
    )

    return GlobalSystemMediaTransportControlsSessionManager


def _run_async(awaitable: Any) -> Any:
    import asyncio

    return asyncio.run(awaitable)


def _status_name(value: Any) -> str:
    """SMTC 播放状态 → `playing|paused|none`（4 播放 / 5 暂停，其余算 none）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "none"
    return _PLAYBACK_NAMES.get(number, "none")


def _media_blank() -> dict[str, Any]:
    return {
        "available": False,
        "app": "",
        "status": "",
        "title": "",
        "seconds": None,
        "end_seconds": None,
    }


_smtc_lock = threading.Lock()
_smtc_cooldown_until = 0.0
_smtc_stuck = 0  # 连续滞留（超时「留下自灭」）计数（票 #181 H4，与 UIA 各自独立）


def _smtc_cooling() -> bool:
    with _smtc_lock:
        return time.monotonic() < _smtc_cooldown_until


def _start_smtc_cooldown(reason: str, stuck: bool = False) -> None:
    """进入 SMTC 冷却；同一段冷却期只记一次日志（与 UIA 的冷却各自独立）。

    票 #181 H4：与 UIA 同一改法 —— 连续滞留达 `STUCK_LIMIT` 改用长冷却。
    """
    global _smtc_cooldown_until, _smtc_stuck
    now = time.monotonic()
    with _smtc_lock:
        if stuck:
            _smtc_stuck += 1
        first = now >= _smtc_cooldown_until
        span = STUCK_COOLDOWN_S if _smtc_stuck >= STUCK_LIMIT else SMTC_COOLDOWN_S
        _smtc_cooldown_until = now + span
    if first:
        log.info("SMTC 探针冷却：%s（%.0fs 内不再读媒体会话）", reason, span)


def _smtc_stuck_clear() -> None:
    """worker 收尾（成功/异常都算线程已退出，没滞留）：滞留计数清零（票 #181 H4）。"""
    global _smtc_stuck
    with _smtc_lock:
        _smtc_stuck = 0


def _run_smtc(
    action: Any, timeout_ms: int, fallback: Any
) -> dict[str, Any]:
    """SMTC 操作的统一保护：冷却门 → 工作线程 + 截止时间 → 超时/异常则冷却并回 `fallback`。

    `media_snapshot` / `media_pause` / `media_resume` 共用这一台机器（三者共享同一条 SMTC
    冷却，但**与 UIA 的冷却相互独立**）。冷却期内不进 worker。日志只记成败与耗时。

    票 #181 H4：与 UIA 同一改法 —— worker 超时滞留计入连续滞留，达 `STUCK_LIMIT`
    这次冷却改长档（线程不再随冷却周期线性累积）；worker 收尾即清零。
    """
    if _smtc_cooling():
        return fallback()
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = action()
        except BaseException as exc:  # noqa: BLE001 - 任何异常都降级
            box["error"] = type(exc).__name__

    worker = threading.Thread(target=work, name="smtc-op", daemon=True)
    worker.start()
    worker.join(max(0.0, timeout_ms / 1000.0))
    if worker.is_alive():
        # 票 #181 H4：超时「留下自灭」= 滞留一条，计入连续滞留（达阈值这次进长冷却）
        _start_smtc_cooldown(f"超时 {timeout_ms}ms", stuck=True)
        return fallback()
    _smtc_stuck_clear()  # 线程收尾（成功/异常都算）：没滞留（票 #181 H4）
    if "error" in box:
        _start_smtc_cooldown(f"异常 {box['error']}")
        return fallback()
    value = box.get("value")
    return value if isinstance(value, dict) else fallback()


async def _media_title(session: Any) -> str:
    try:
        props = await session.try_get_media_properties_async()
    except Exception:  # noqa: BLE001 - 标题拿不到不影响门里的 app 判定
        return ""
    return str(getattr(props, "title", "") or "") if props is not None else ""


def _bare_title(value: Any) -> str:
    """去空白 + casefold 的标题（只用于「逐字相同」判定）。"""
    return _SPACE.sub("", str(value or "")).casefold()


async def _session_matches(session: Any, app: Any, title: Any) -> bool:
    """§3 同一套门：app token 同类 + 标题宽松匹配。"""
    if not same_app(app, session.source_app_user_model_id):
        return False
    media_title = await _media_title(session)
    # 逐字相同一定算命中：续播带的是暂停时记下的**真实会话标题**（分P 时它和窗口
    # 标题不同，且可能短于 TITLE_LOOSE_MIN），不该被宽松门的最小长度卡掉。
    bare = _bare_title(media_title)
    if bare and bare == _bare_title(title):
        return True
    return loose_title_match(media_title, title)


def _sessions(manager: Any) -> list[Any]:
    """全部媒体会话（`get_sessions()` 是 winrt 的 `IVectorView`：`.size` + `.get_at(i)`）。"""
    try:
        view = manager.get_sessions()
        return [view.get_at(index) for index in range(view.size)]
    except Exception:  # noqa: BLE001 - 拿不到就当一个都没有
        return []


async def _session_snapshot(session: Any) -> dict[str, Any]:
    """某个会话的快照（§2.2 的形状：available/app/status/title/seconds/end_seconds）。"""
    timeline = session.get_timeline_properties()
    end_seconds = _to_seconds(getattr(timeline, "end_time", None))
    return {
        "available": True,
        "app": str(session.source_app_user_model_id or ""),
        "status": _status_name(session.get_playback_info().playback_status),
        "title": await _media_title(session),
        "seconds": position_seconds(
            {
                "position": _to_seconds(getattr(timeline, "position", None)),
                "end_time": end_seconds,
            }
        ),
        "end_seconds": end_seconds,
    }


def _solo_same_status(sessions: Iterable[Any], app: Any, prefer: str) -> list[Any]:
    """同 app 且状态为 `prefer` 的会话**恰好一只**时回它，否则回空（宁缺勿错）。"""
    same = [
        session
        for session in sessions
        if same_app(app, session.source_app_user_model_id)
        and _status_name(session.get_playback_info().playback_status) == prefer
    ]
    return same if len(same) == 1 else []


async def _pick_session(
    manager: Any, app: Any, title: Any, prefer: str, *, video_solo: bool = False
) -> Any | None:
    """按门挑会话（v5 §2.2/§3 复核补强）：**不只看 `CurrentSession`**。

    遍历全部会话，取「app 同类 + 标题宽松匹配」的；多个候选时优先 `prefer`
    （位置/暂停要 `playing`，续播要 `paused`），仍并列就取第一个。挑不到 → `None`。

    v5 §2.2/§3 修订（2026-10-23）：B站**分P**视频的窗口标题是分P名、SMTC 标题是整部
    视频名，标题门永远对不上 → 呼出不暂停、位置不填。此时（且仅此时）`video_solo=True`
    允许接受「同 app 且恰好一只处于 `prefer` 状态」的会话；调用方只在标题像视频站
    （暂停/续播）或 URL 是播放页（位置）时才传 True，多候选一律不猜。
    """
    sessions = _sessions(manager)
    candidates = [
        session for session in sessions if await _session_matches(session, app, title)
    ]
    if not candidates and video_solo:
        candidates = _solo_same_status(sessions, app, prefer)
    if not candidates:
        return None
    if len(candidates) > 1:
        preferred = [
            session
            for session in candidates
            if _status_name(session.get_playback_info().playback_status) == prefer
        ]
        if preferred:
            return preferred[0]
    return candidates[0]


async def _snapshot_async(manager_cls: Any) -> dict[str, Any] | None:
    """**当前会话**快照（设置页卡片用，§2.2 保持现状）。"""
    manager = await manager_cls.request_async()
    session = manager.get_current_session() if manager is not None else None
    if session is None:
        return None
    return await _session_snapshot(session)


async def _matching_snapshot_async(
    manager_cls: Any, app: Any, title: Any, prefer: str, video_solo: bool = False
) -> dict[str, Any] | None:
    """按门挑一个会话并取快照；挑不到 → `None`。"""
    manager = await manager_cls.request_async()
    if manager is None:
        return None
    session = await _pick_session(manager, app, title, prefer, video_solo=video_solo)
    if session is None:
        return None
    return await _session_snapshot(session)


def _media_snapshot_now() -> dict[str, Any]:
    """当前会话快照本体（在 worker 线程里跑）。依赖缺失 → 空白（快，不冷却）；异常往上抛。"""
    try:
        manager_cls = _manager_class()
    except Exception:  # noqa: BLE001 - winrt 缺席：静默降级，不算「失败」
        return _media_blank()
    info = _run_async(_snapshot_async(manager_cls))
    if info is None:
        return {**_media_blank(), "available": True, "status": "none"}
    return info


def _matching_snapshot_now(
    app: Any, title: Any, prefer: str, video_solo: bool = False
) -> dict[str, Any]:
    """按门挑会话的快照本体（在 worker 线程里跑）；挑不到 → 空白（available=False）。"""
    try:
        manager_cls = _manager_class()
    except Exception:  # noqa: BLE001
        return _media_blank()
    info = _run_async(
        _matching_snapshot_async(manager_cls, app, title, prefer, video_solo=video_solo)
    )
    return info if info is not None else _media_blank()


def media_snapshot(
    app: Any = None,
    title: Any = None,
    *,
    prefer: str = "playing",
    video_solo: bool = False,
) -> dict[str, Any]:
    """媒体会话快照，与 UIA 同一套保护：工作线程 + 截止时间 + 失败冷却。

    **两种口径不等价，别混用**：

    - **无参**：取 `CurrentSession` —— 设置页在拿不到「按门」上下文（没有暂停记录）时的
      **兜底**；多会话并存时它可能是**另一只**会话。
    - **带 app/title**：遍历全部会话**按门挑**（app 同类 + 标题宽松匹配，多个候选优先
      `prefer`）—— 媒体联动/暂停记录用它挑「我们暂停的那只」，挑不到 → 空白
      （`available=False`）。
    - **`video_solo=True`**（v5 §2.2/§3 修订）：标题门对不上时放开「同 app 唯一播放中的
      会话」兜底 —— 只给**位置自动填**用（调用方已确认 URL 是视频站播放页）；设置页展示
      保持严格门，不因兜底说错话。

    截止时间内没回来 / 抛异常 → `{"available": False, …}` 并冷却 30s（与 UIA 冷却独立）。
    """
    if app is None and title is None:
        return _run_smtc(_media_snapshot_now, SMTC_TIMEOUT_MS, _media_blank)
    return _run_smtc(
        lambda: _matching_snapshot_now(app, title, prefer, video_solo),
        SMTC_TIMEOUT_MS,
        _media_blank,
    )


async def _pause_async(manager_cls: Any, app: Any, title: Any) -> dict[str, Any]:
    manager = await manager_cls.request_async()
    # 挑会话：多个候选优先正在播放的那个（§2.2/§3 同一条挑法）；
    # 标题像视频站时才允许分P兜底（v5 §2.2/§3 修订）
    session = (
        await _pick_session(
            manager, app, title, "playing", video_solo=looks_like_video_title(title)
        )
        if manager
        else None
    )
    if session is None:
        return {"ok": True, "paused_by_us": False, "status": "none"}
    status = _status_name(session.get_playback_info().playback_status)
    if status != "playing":
        return {"ok": True, "paused_by_us": False, "status": status}
    media_title = await _media_title(session)
    try:
        paused = bool(await session.try_pause_async())
    except Exception:  # noqa: BLE001
        return {"ok": False, "paused_by_us": False, "status": status}
    result: dict[str, Any] = {
        "ok": True,
        "paused_by_us": paused,
        "status": "paused" if paused else status,
    }
    if paused:
        # 分P 时真实会话标题与窗口标题不同；收工续播要靠它挑回同一只。
        result["session_app"] = str(session.source_app_user_model_id or "")
        result["session_title"] = media_title
    return result


async def _resume_async(
    manager_cls: Any, app: Any, title: Any, paused_by_us: bool
) -> dict[str, Any]:
    manager = await manager_cls.request_async()
    # 续播侧优先挑「暂停着」的那个（我们暂停过的那条多半是它）。正常路径上
    # hub 送的就是暂停时记下的真实会话标题（逐字命中）；送窗口标题时（结果未知的
    # 悲观置位、跨重启遗留）依然允许分P兜底。
    session = (
        await _pick_session(
            manager, app, title, "paused", video_solo=looks_like_video_title(title)
        )
        if manager
        else None
    )
    if session is None:
        return {"ok": True, "paused_by_us": False, "status": "none"}
    status = _status_name(session.get_playback_info().playback_status)
    # 没压过暂停 → 不动、不留痕迹；用户自己续播了 → 只清标志
    if not paused_by_us or status != "paused":
        return {"ok": True, "paused_by_us": False, "status": status}
    try:
        played = bool(await session.try_play_async())
    except Exception:  # noqa: BLE001
        return {"ok": False, "paused_by_us": False, "status": status}
    return {"ok": True, "paused_by_us": False, "status": "playing" if played else status}


def _media_pause_now(app: Any, title: Any) -> dict[str, Any]:
    """暂停本体（在 worker 线程里跑）：门 + 只对 `playing` 动手。"""
    try:
        manager_cls = _manager_class()
    except Exception:  # noqa: BLE001 - 依赖缺失：快、不冷却
        return {"ok": False, "paused_by_us": False, "status": "unavailable"}
    return _run_async(_pause_async(manager_cls, app, title))


def _media_resume_now(app: Any, title: Any, paused_by_us: bool) -> dict[str, Any]:
    """续播本体（在 worker 线程里跑）：只有我们暂停过且仍暂停才动手。"""
    try:
        manager_cls = _manager_class()
    except Exception:  # noqa: BLE001
        return {"ok": False, "paused_by_us": paused_by_us, "status": "unavailable"}
    return _run_async(_resume_async(manager_cls, app, title, paused_by_us))


def media_pause(app: Any, title: Any) -> dict[str, Any]:
    """呼出时暂停（§3）：门 + 只对 `playing` 动手。工作线程 + 截止时间 + 冷却。

    超时/异常 → `{"ok": False, "paused_by_us": False, "status": "unavailable"}`；
    `ok:false` 只在依赖缺失/超时/异常时出现（hub 侧据此决定是否悲观置位）。
    真暂停成功时回包额外带 `session_app` / `session_title`（被暂停那只的**真实**身份，
    分P 时与窗口标题不同）—— hub 用它跨重启续播同一只。
    """
    return _run_smtc(
        lambda: _media_pause_now(app, title),
        SMTC_TIMEOUT_MS,
        lambda: {"ok": False, "paused_by_us": False, "status": "unavailable"},
    )


def media_resume(app: Any, title: Any, *, paused_by_us: bool = False) -> dict[str, Any]:
    """收工续播（§3）：**只有我们暂停过**（`paused_by_us`）且仍暂停才动手。

    `paused_by_us` 由 hub 在内存里持有（唯一真源），这里只是执行者 —— 门与暂停共用一套。
    同样走工作线程 + 截止时间 + 冷却；超时/异常 → `ok:false`（hub 侧保留标志与状态文件）。
    """
    flag = bool(paused_by_us)
    return _run_smtc(
        lambda: _media_resume_now(app, title, flag),
        SMTC_TIMEOUT_MS,
        lambda: {"ok": False, "paused_by_us": flag, "status": "unavailable"},
    )


# --- 组合：探针产出 §2.1 的 browser 对象 --------------------------------------


def probe_url(
    hwnd: int, app: Any, title: Any, timeout_ms: int = UIA_TIMEOUT_MS
) -> dict[str, Any] | None:
    """读地址栏拿 URL（读不到 → `None`），再用 SMTC 补位置。位置只在匹配到同一会话时用。"""
    url = read_address_bar(hwnd, timeout_ms)
    if not url:
        return None
    seconds: float | None = None
    has_video = False
    try:
        # 按门挑会话（§2.2 复核补强）：挑到才取位置；挑不到 → available=False。
        # v5 §2.2/§3 修订：URL 是视频站播放页时允许分P兜底（标题门对不上但确实是同一只）。
        snapshot = media_snapshot(app, title, video_solo=is_video_page(url))
    except Exception:  # noqa: BLE001
        snapshot = {"available": False}
    if snapshot.get("available"):
        has_video = True
        seconds = snapshot.get("seconds")
    return {
        "url": url,
        "site": site_of(url),
        "has_video": has_video,
        "seconds": seconds,
        "time_text": format_timestamp(seconds),
        "source": "probe",
    }


def probe_available() -> bool:
    """UIA 探针可用？（依赖在位 = comtypes + UIAutomationCore.dll）。"""
    try:
        import ctypes

        import comtypes  # noqa: F401 - 只要导入成功即可

        ctypes.WinDLL("UIAutomationCore.dll")
    except Exception:  # noqa: BLE001
        return False
    return True


def media_available() -> bool:
    """媒体联动可用？（winrt 的 SMTC 能导入）。"""
    try:
        _manager_class()
    except Exception:  # noqa: BLE001
        return False
    return True


# --- 冷启动预热（v5 §2.2 复核补强）--------------------------------------------

_warm_lock = threading.Lock()
_warmed = False


def _warm_uia() -> None:
    """触发 comtypes 生成 UIAutomationClient 模块 + 建一次 UIA 对象。

    实测首次生成可能 >800ms —— 正是「新装/重启后第一次取词拿不到链接」的元凶。
    """
    import comtypes.client

    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as UIA

    comtypes.client.CreateObject(UIA.CUIAutomation)


def warm_up() -> bool:
    """后台预热（v5 §2.2）：UIA 模块生成 + SMTC 投影载入。**幂等、不抛、只记一行日志**。

    返回是否完成；依赖缺失也静默（只是探针/联动不可用）。失败不置「已预热」，允许下次重试。
    """
    global _warmed
    with _warm_lock:
        if _warmed:
            return True
    started = time.perf_counter()
    ok = True
    try:
        _warm_uia()
    except Exception as exc:  # noqa: BLE001 - 预热失败只降级，不影响取词
        ok = False
        log.info("UIA 预热失败：%s（%dms）", type(exc).__name__, _ms(started))
    try:
        _manager_class()  # 载入 SMTC 投影（失败无所谓：媒体联动不可用而已）
    except Exception:  # noqa: BLE001
        pass
    if ok:
        with _warm_lock:
            _warmed = True
        log.info("预热完成（%dms）", _ms(started))
    return ok
