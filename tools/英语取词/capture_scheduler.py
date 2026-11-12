"""采集编排调度器（v33 T5，票 #211 / spec §B2 §C10）：心跳 → 触发/停止/降级调度。

**职责边界**（与 T1/T2 的分工，spec §B2b）：

- **触发**：复用既有浏览器上下文心跳（extension/cs.js 每 2s 一条
  `href/hasVideo/currentTime/paused`）判定「B站视频页 + 正在播放」→ 会话开始
  查一次 CC 轨（多 P 从 href 解析 `p` 参数）→ 有英文轨整轨落库（T1
  `cc_track.collect`），**OCR 采样不启动**；无轨/仅 ai-zh → 启动 T2 采样循环。
- **停止**：停播 / 切页 / 换视频即停（flush），恢复播放续采；换视频 → 新会话
  （待选句按会话隔离，归户走既有 sessions/store 规则，`ensure_session` 注入）。
- **降级**（spec §B2c）：查轨失败（网络/接口/坏 JSON —— `cc_track` 已静默返回
  reason 码）→ 静默转采样，流程不断；采样失败（引擎建不起来 / 抓带失败）同理；
  任一来源故障不中断整链。ASR 二期未接入，调度按 CC > (ASR 缺位) > OCR 自然退化
  —— `cc_done` 段内压制采样就是「更高优先级来源在场」的占位语义，ASR 合流时在
  查轨与采样之间插入同构分支即可。
- **总开关**：`enabled` 旗标（hub 的 `reload_switches` 从 config 的 `video_capture`
  下推）。关 = 完全静默（零查轨零采样），拨关瞬间把活动段 flush 干净。
- **手动扳机**（v34 T3，票 #216）：`armed` 是**内存态**（默认 False，中枢重启即归零），
  与 `enabled` 组成双门 —— `enabled && armed` 才允许查轨与采样。`off` 持锁立即
  flush + 丢弃当前段；置位后连续 `ARM_IDLE_TTL_S`（10 分钟）没有播放心跳自动清零。
- **状态出口**（v34 T5，票 #218 / spec §C19–C24）：`status(heartbeat_age_ms)` 回全局
  相位（11 档；不需要先知道会话 id）、本段落库句数、最近落库时间与关键事件环；
  `dormant` 是显式布尔位（`status_of` 的 `ocr` 也报它，不再谎报 active）；
  事件冷却表 `_cooldowns` 在实例上（跨段共享），段内去重随 `_VideoState` 丢弃。

**状态机**（每条视频一个分段 `_VideoState`）：

    心跳 → enabled? → armed? → (B站视频页 + 播放中)?
      ├─ 否 → 活动段在就 flush + 挂起（回来续采）
      └─ 是 → 段内 id（BV 号+分P）变了？ → flush + 丢弃旧段
              → 未查过轨：建会话 → collect_cc（reason=None → cc_done，不采样）
              → 未 cc_done：建采样器（引擎建不起 → 本段记败）→ 喂帧/休眠
              → 周期 flush（FLUSH_PERIOD_S）+ needs_redrag 每段报一次

**线程模型**：心跳来自 HTTP 线程池（多标签页/重试可并发），`on_heartbeat` 全程
持锁串行（喂帧含 OCR ~700ms，后到的心跳排队即可，宁慢不乱）。日志纪律（§10）：
只记动作与数量，不记 URL / 标题 / 句文本。

测试：`tests/test_capture_scheduler.py` 全离线（假心跳序列 → 断言动作账目）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from ocr_sampler import SAMPLE_PERIOD_S, video_rect_from_context
from provenance import video_id_of
from sessions import format_ts, is_video_page

log = logging.getLogger("hub.capture_scheduler")

# 周期 flush（T2 交付口径「T5 可周期调用（如每 60s）或停播时调用」）：
# 投票组在带子不变时不增长，60s 落一次池足够实时；停播/切页/换视频另有即时 flush。
FLUSH_PERIOD_S = 60.0

# v34 T3（票 #216 / spec §B13）：空闲自解的阈值 —— `armed` 置位后连续这么久没有
# 「播放心跳」（B站视频页 + paused=false）就自动清零，避免第二天突然开始采。
# 任何 tick 都参与判定（含 focused:false / 停播心跳）；页面彻底没心跳时只能等
# 下一拍再判，属已知边界（没有心跳就没有 tick，调度器不自己起定时器）。
ARM_IDLE_TTL_S = 600.0

# 2026-11-10：播放状态**粘性窗口**（秒）。B站 播放页会同时从多个 frame / 预览
# video / 失焦补发处各发一条心跳，`paused` 互相矛盾，两秒一条交替到达。同页最近
# 这么久内见过 playing=true 就继续采，把抖动抹平；用户真暂停时最多晚这么多秒挂起。
# 取值理由：心跳间隔 ~2s，3s ≈ 1.5 个周期 —— 够跨过一次抖动，又不至于让「真暂停」
# 拖太久（暂停期间采到的还是同一句，去重会挡掉重复落库）。
PLAY_STICKY_S = 3.0

# 2026-11-10：「无画面字源」观测窗（秒）。用户做无字幕英语听力练习时，
# 既没有英文 CC 轨、画面也没有烧录字幕 —— 这类片源在 ASR 二期接入前拿不到
# 任何文本，桌宠却一直按「有字幕只是没框」问用户拖框。窗口内一帧有字行签名
# 都没见过就判「这条没有可读画面字源」，走临时保底（明说 + 自动停收句）。
#
# 为什么不能看 `added`：`FLUSH_PERIOD_S = 60`，正常有字幕的片头 60 秒落库句数
# 也恒为 0，10 秒窗口里用「零句」判据会把所有正常视频一起误杀。所以观测的是
# **帧级签名**（`FeedReport.sig_ok`），与落库节奏完全解耦。
# 10 秒 ≈ 5 帧 @2s 心跳；用户拍板「宁可早停、别再纠缠」。
NO_SOURCE_WINDOW_S = 10.0

# v34 T5（票 #218 / spec §C20）：心跳年龄超过这个阈值 = `no_heartbeat`。
# 「心跳即 tick」：扩展只在页面聚焦时上报（extension/cs.js），没有心跳时
# `_suspend()` 永远不会执行，状态会冻结在 active —— 相位判定必须看年龄。
HEARTBEAT_STALE_MS = 10_000

# v34 T5（spec §C24）：关键事件的实例级冷却（跨段共享，`_drop_state` 不清）。
# 五类事件里调度器负责 start / stop / fallback / redrag（save 由 web 的保存
# toast 负责）；5 分钟内同一类事件最多报一次，避免换视频反复弹。
EVENT_COOLDOWN_S = 300.0
EVENT_KEEP = 20  # 事件环大小（web 按 id 增量消费）

# on_heartbeat 的动作账目词表（测试断言的口径）
A_COLLECT = "collect_cc"
A_START = "start_sampling"
A_FEED = "feed"
A_SKIP = "skip_feed"
A_DORMANT = "dormant"
A_FLUSH = "flush"
A_PAUSE = "pause"
A_RESUME = "resume"
A_CHANGE = "video_change"
A_NO_SESSION = "no_session"
A_NEEDS_REDRAG = "needs_redrag"
A_IDLE = "idle"
A_ARM_IDLE = "arm_idle"  # v34 T3：空闲自解（扳机自动清零）

# 用户代理 → 浏览器友好名（与 lib/Logic.ahk 的 FriendlyApp 映射表同一批名字）：
# 会话键 = FriendlyApp + CleanTitle —— 调度器建的会话必须和外壳取词建的会话撞在
# 同一个键上（同一只浏览器看同一页 → 归同一会话），否则同一条视频会裂成两个会话。
# Edge 的 UA 同时含 Chrome/ 与 Edg/ —— Edg/ 必须先判。
_UA_RULES: tuple[tuple[str, str], ...] = (
    ("Edg/", "Edge"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
)


# --- 纯函数：身份判定 ------------------------------------------------------------


def browser_app_from_ua(user_agent: Any) -> str:
    """HTTP User-Agent → 浏览器友好名（Edge/Chrome/Firefox）；认不出 → 空串。

    空串进 `session_key` 会落在「`|标题`」键上，与外壳取词的键（`Edge|标题`）永远
    不等 —— 调用方（hub）拿不到像样的 UA 时宁可让会话归户不到也不猜错名。
    """
    text = str(user_agent or "")
    for token, app in _UA_RULES:
        if token in text:
            return app
    return ""


def bvid_and_page(href: Any) -> tuple[str, int] | None:
    """B站视频播放页 → `(BV号, 分P)`；不是播放页 / 域不对 / 无 BV 号 → None。

    分 P 从 href 的 `p` 参数解析（缺省 1）；多 P 的 cid 由 hub 侧查轨适配器按
    `view.data.pages[k].cid` 解析（spec §B7：轨按分 P 粒度）。
    """
    url = str(href or "").strip()
    if not url or not is_video_page(url):
        return None
    bvid = video_id_of(url)
    if not bvid:
        return None
    page = 1
    try:
        raw = (urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("p") or [""])[0]
        if raw:
            page = int(raw)
    except ValueError:
        page = 1
    if page < 1:
        page = 1
    return bvid, page


def playing_of(ctx: Mapping[str, Any]) -> bool:
    """心跳在播放吗？`hasVideo` 为真且 `paused` 明确为 False（视频末尾 paused 会翻 true）。"""
    return bool(ctx.get("hasVideo")) and ctx.get("paused") is False


# 判定「播完了」的容差：B站 末尾 `currentTime` 常差几百毫秒不到 `duration`。
END_TOLERANCE_S = 2.0


def ended_of(ctx: Mapping[str, Any]) -> bool:
    """这条视频是不是**播到末尾停住了**（v36 追加，2026-11-10 用户要求自动停收句）。

    只有当三个条件同时成立才算「播完」，宁漏不错：

    1. `hasVideo` 为真（页面里真有 `<video>`）；
    2. `paused` 明确为 true —— 播完时浏览器会翻 true，中途暂停也翻 true，所以还要
       第 3 条把两者分开；
    3. `currentTime` 已经到 `duration - END_TOLERANCE_S` 以内 —— **这是把「播完」
       和「看到一半手动暂停」分开的唯一凭据**：中途暂停离末尾还远。

    `duration` 缺失 / 为 0 / `NaN`（直播流、还没拿到元数据）一律判否：没有总长就
    谈不上「到尾」。`currentTime` 缺失同理。

    注意与 `playing_of` 的关系：播完的瞬间 `paused` 翻 true，于是 `playing_of` 也变
    False、场景回落「未播放」。两者不冲突——`playing_of` 回答「在播吗」，
    `ended_of` 回答「是播完了还是暂停在中途」。
    """
    if not bool(ctx.get("hasVideo")):
        return False
    if ctx.get("paused") is not True:
        return False
    duration = ctx.get("duration")
    current = ctx.get("currentTime")
    if not isinstance(duration, (int, float)) or not isinstance(current, (int, float)):
        return False
    if isinstance(duration, bool) or isinstance(current, bool):   # JSON 的 true/false
        return False
    if duration <= 0:
        return False
    return float(current) >= float(duration) - END_TOLERANCE_S


# --- 纯函数：场景选槽（v36 决策 2/3）-----------------------------------------------
#
# 「谁最后说话」与「谁在播视频」是两件事：`adopt_browser`（取词归属）要的是前者，
# 场景邀请要的是后者。这里只回答后者，且用**场景自己的**新鲜度预算。

# 场景专用新鲜度（宽）。与取词归属的 `sessions.BROWSER_CONTEXT_TTL_MS`（30s）
# **各自独立**：放宽「我知道你在看这个视频」不许顺带放宽「这次取词该归到哪个会话」
# （决策 3）。5 分钟 = 既有 `BROWSER_BLUR_GRACE_MS` 的量级。
SCENE_FRESH_MS = 300_000

# 多槽上限：按页面身份（href 归一化）保留，8 个够覆盖「双屏 + 一堆标签页」；超了按
# 「最久没说话」淘汰 —— 场景只看 SCENE_FRESH_MS 内的，扔掉的都是早就过期的槽。
SCENE_SLOT_MAX = 8


def context_slot_key(href: Any) -> str:
    """页面身份（v36 决策 2）：href 归一化成一个槽键。

    - 只去 **fragment**（播放页的 `#reply` 之类会随交互变）与 host 大小写；
    - query **原样保留、不排序**（`?p=2` 是另一个分 P，属于另一个页面身份）；
    - 去掉末尾多余的 `/`（`/video/BV1xx/` 与 `/video/BV1xx` 是同一页）；
    - 非 http(s) / 空 / 解不出来的串 → 回落到「trim 后原样」，绝不抛。
    """
    text = "" if href is None else str(href).strip()
    if not text:
        return ""
    try:
        parts = urllib.parse.urlsplit(text)
        host = (parts.hostname or "").lower()
        if not host:
            return text
        path = (parts.path or "").rstrip("/")
        query = f"?{parts.query}" if parts.query else ""
        return f"{parts.scheme.lower()}://{host}{path}{query}"
    except ValueError:
        return text


def _slot_age_ms(slot: Mapping[str, Any]) -> float | None:
    """槽快照里的 `age_ms`（非法 / 负数 → None，调用方跳过这一槽）。"""
    try:
        age = float(slot.get("age_ms"))
    except (TypeError, ValueError, AttributeError):
        return None
    return age if age >= 0 else None


def pick_scene_context(
    slots: Any, *, fresh_ms: int = SCENE_FRESH_MS
) -> dict[str, Any] | None:
    """场景用上下文（v36 决策 2/3）：**最近仍在播视频的那一槽**，没有就回落最新鲜的槽。

    输入是 hub `browser_slots()` 的槽快照（`{key, context, age_ms, focused}`，顺序无关）。
    两层选择：

    1. 在 `fresh_ms` 内的槽里找「B站视频页 + hasVideo + 没暂停」的（判据复用
       `bvid_and_page` / `playing_of`，不新增真值），取 `age_ms` 最小的那个
       —— 同时开着视频窗口与其它页面时，认得出**正在播的那一个**；
    2. 一个在播的都没有 → 回落**最新鲜**的那一槽：暂停 / 非视频页仍要如实回答
       `video` / `playing` 的原始观测（`scene_of` 的既有契约）。

    **故意不看 `focused`**：焦点不是「在播」的判据（决策 1）。全都不新鲜 → `None`。
    """
    freshest: Mapping[str, Any] | None = None
    freshest_age: float | None = None
    playing: Mapping[str, Any] | None = None
    playing_age: float | None = None
    for slot in slots or ():
        if not isinstance(slot, Mapping):
            continue
        age = _slot_age_ms(slot)
        if age is None or age > fresh_ms:
            continue
        if freshest_age is None or age < freshest_age:
            freshest, freshest_age = slot, age
        ctx = slot.get("context")
        if not isinstance(ctx, Mapping):
            continue
        if bvid_and_page(ctx.get("href")) is None or not playing_of(ctx):
            continue
        if playing_age is None or age < playing_age:
            playing, playing_age = slot, age
    chosen = playing if playing is not None else freshest
    return dict(chosen) if chosen is not None else None


# 相位词表（spec §C19 的 11 个互斥值）。
PHASES: tuple[str, ...] = (
    "unarmed",
    "no_heartbeat",
    "cc_reading",
    "cc_ok",
    "cc_missing",
    "cc_error",
    "sampling",
    "paused",
    "dormant",
    "redrag_needed",
    "failed",
)


def phase_of(
    *,
    enabled: bool,
    armed: bool,
    heartbeat_age_ms: int | None,
    redrag: bool = False,
    paused: bool = False,
    dormant: bool = False,
    failed: bool = False,
    sampling: bool = False,
    cc_ok: bool = False,
    cc_missing: bool = False,
    cc_error: bool = False,
    cc_reading: bool = False,
) -> str:
    """相位判定（纯函数，spec §C19）。

    优先级：`unarmed`（开关关 / 未按开始）> `no_heartbeat`（心跳年龄超阈值）>
    `redrag_needed` > `paused` > `dormant` > 其余（`failed` > `sampling` >
    `cc_ok` > `cc_missing` > `cc_error` > `cc_reading`）。都没有 = `paused`
    （有心跳但没有活动段 = 等待播放）。

    `cc_missing` / `cc_error` 是降级决策的瞬时相位；采样真正跑起来后归
    `sampling`（用户看视频时最关心的「现在在不在采」）。
    """
    if not enabled or not armed:
        return "unarmed"
    if heartbeat_age_ms is None or heartbeat_age_ms > HEARTBEAT_STALE_MS:
        return "no_heartbeat"
    if redrag:
        return "redrag_needed"
    if paused:
        return "paused"
    if dormant:
        return "dormant"
    if failed:
        return "failed"
    if sampling:
        return "sampling"
    if cc_ok:
        return "cc_ok"
    if cc_missing:
        return "cc_missing"
    if cc_error:
        return "cc_error"
    if cc_reading:
        return "cc_reading"
    return "paused"


def no_source_window(
    *,
    now: float,
    window_start: float,
    sig_hit: bool,
    frames_seen: int,
    threshold: float = NO_SOURCE_WINDOW_S,
) -> bool:
    """10 秒观测窗到期、且全程没识别出过一行可读文本 → 判「无画面字源」（纯函数）。

    `sig_hit` 一旦为真即**本段永久放行**：片头识别出一行真字幕就足以证明这条片源
    有画面文本，后面的空镜/擦肩不该再把收句停掉（用户拍板「一帧就不停」）。
    `frames_seen > 0` 才允许触发 —— ping 可能抢在第一个喂帧 tick 之前到达；
    `window_start <= 0` 表示还没起窗（建段但尚未喂帧、或刚挂起重开）。
    """
    if sig_hit or frames_seen <= 0 or window_start <= 0:
        return False
    return now - window_start >= threshold


def no_source_eligible(
    *,
    armed: bool,
    session_id: int | None,
    cc: str,
    source_available: bool,
    window_expired: bool,
) -> bool:
    """`cap_no_source` 的准入闸（纯函数）。

    - `cc` 只认「无英文轨」与「查轨故障」两条降级路径；`ok`（英文 CC 整轨在库）
      与 `off`（来源尚未判明）都不该触发。
    - `source_available` 是**给 ASR 二期留的豁免位**：本轮恒 False，P2 的 ASR 段
      置 True 后本判定恒 False —— 那时「无 CC + 无画面字幕」是有文本来源的，
      临时保底必须自动让位，hub / 外壳接线一字不用改。
    """
    if not armed or session_id is None or int(session_id) <= 0:
        return False
    if cc not in ("no_english_track", "error"):
        return False
    if source_available:
        return False
    return window_expired


# --- 状态机 ----------------------------------------------------------------------


class _VideoState:
    """一个视频分段（BV 号+分P）的全部调度状态。"""

    def __init__(
        self,
        *,
        identity: tuple[str, int],
        href: str,
        title: str,
        now: float,
    ) -> None:
        self.identity = identity
        self.href = href
        self.title = title
        self.session_id: int | None = None
        self.cc_checked = False  # 本段查过轨（无论结果，spec §B8b 只查一次）
        self.cc_done = False  # 英文 CC 已整轨落库 → 采样压制
        self.cc_reason: str | None = None  # 查轨未落库的原因码（cc_track 的 reason）
        self.sampler: Any | None = None
        self.sampler_failed = False  # 引擎建不起来（本段不再逐 tick 重试）
        self.suspended = False  # 停播/切页挂起中（sampler 对象保留，恢复续采）
        self.region: tuple[int, int, int, int] | None = None
        # v34 T5（票 #218 / spec §C20）：无区域休眠是**显式布尔位**，不用
        # `region is None` 反推（停播挂起时 region 也可能尚未加载，会把暂停误报成
        # 「等你拖框」）。置位只在 `_feed_tick` 落 A_DORMANT 那一支；清位在
        # region 加载成功 / `redrag()` 成功 / `_suspend()` 三处。
        self.dormant = False
        self.last_flush = now
        # v34 T5（spec §C24）：本段 flush 落库新增句数累计 + 最近一次有新增的时间
        # （`status()` 的 `added_in_segment` / `last_added_at`，换段随段归零）。
        self.added = 0
        self.last_added_at: str | None = None
        # 本段已经报过的事件 kind（「同一段同一事件只报一次」的段内那一层；
        # 跨段的 5 分钟冷却在 CaptureScheduler._cooldowns 上）。
        self.events_reported: set[str] = set()
        self.redrag_reported = False
        self.dormant_reported = False  # 「无区域休眠」每段只 INFO 提示一次（防刷屏）
        # 2026-11-10：10 秒「无画面字源」观测窗（cap_no_source 的唯一真值）。
        # 起窗在**首个喂帧 tick**（不是建段）—— 查轨/建采样器的耗时不该吃掉窗口。
        self.window_start = 0.0
        self.frames_seen = 0
        # 本段是否**真识别出过可读拉丁行**（FeedReport.lines > 0）。不是 ink 签名：
        # 控件白字/亮场景会让签名误过，那样永远判不出「无字源」（2026-11-11 真机）。
        self.sig_hit = False
        # ASR P2 的豁免位：无字源窗口到期、转写已排队 → 置 True，10 秒保底让位。
        self.source_available = False
        # 2026-11-11：当前区域是不是「自动/复用」来的（True = 还没被用户拖过，
        # 外壳据此在开始采样时画一次红框预览；用户重拖后置 False）。
        self.band_auto = False
        # 2026-11-11 区域校正：上一拍视频内容框（物理像素）。框变了就按相对区域/
        # 默认带重算，跟随页面滚动、影院模式与窗口移动。
        self.video_rect: tuple[int, int, int, int] | None = None
        # 2026-11-11：OCR 先行时，查轨放在后台线程；`cc_pending` = 在飞，
        # `cc_result` = 线程回包（tick 消费后清空）。查轨失败也回原因码，不抛。
        self.cc_pending = False
        self.cc_result: Mapping[str, Any] | None = None
        # 2026-11-11 ASR P2：本段是否已试过排队转写（同一段只排一次）。
        self.asr_attempted = False
        # 判定首次成立时只打一行 INFO（ping 是 1Hz，不能在读方法里刷屏）。
        self.no_source_reported = False


class CaptureScheduler:
    """心跳驱动的采集调度器。全部外部依赖注入，测试零网络零真 OCR。

    注入点（hub 用 `build_default_scheduler` 装配，测试用假件）：

    - `ensure_session(title, app, href) -> int | None`：归户/新建视频会话并把视频
      href 落进会话（spec 用户故事 16 的跳回链接；默认实现 `ensure_video_session`）。
      失败回 None → 本 tick 记 `no_session`，下个心跳重试；
    - `collect_cc(session_id, bvid, page) -> Mapping`：查轨整轨落库（T1 组合入口，
      多 P 的 cid 解析在适配器里）；回包按 `reason` 判成败（None=落库成功）；
    - `create_sampler(session_id) -> 带 feed/flush/needs_redrag/set_region 的对象
      或 None`：T2 采样器（引擎建不起来回 None）；
    - `load_region() -> (x, y, w, h) | None`：第一档「复用上次区域」（旧扩展兜底）；
    - `region_from_ctx(ctx) -> (x, y, w, h) | None`：2026-11-11 区域校正 —— 用
      `ctx["videoRect"]`（视频内容框，物理像素）+ 相对区域/默认带算当前区域；
      回 None 时退回 `load_region()`；
    - `grab_band(region) -> band | None`：屏幕抓字幕带（失败回 None = 跳帧）；
    - `screen_size() -> (w, h) | None`：重拖入账要按分辨率落键；
    - `region_saver(region, screen_w, screen_h) -> 四元组`：无活动段时把重拖区域
      持久化；
    - `clock() -> float`：周期 flush 的墙钟（测试注入假钟）；
    - `enabled`：总开关旗标（hub 的 `reload_switches` 下推，默认开）；
    - `armed`：手动扳机（v34 T3，内存态，默认关；中枢重启 = 新实例 = 归零）。
      `enabled && armed` 才允许查轨与采样；`armed` 的置位/清零走 `capture_arm()`。
    """

    def __init__(
        self,
        *,
        ensure_session: Callable[[str, str, str], int | None],
        collect_cc: Callable[[int, str, int], Mapping[str, Any]],
        create_sampler: Callable[[int], Any | None],
        load_region: Callable[[], tuple[int, int, int, int] | None],
        grab_band: Callable[[tuple[int, int, int, int]], Any],
        screen_size: Callable[[], tuple[int, int] | None],
        region_saver: Callable[[tuple[int, int, int, int], int, int], tuple[int, int, int, int]],
        region_from_ctx: Callable[[Mapping[str, Any]], tuple[int, int, int, int] | None]
        | None = None,
        clock: Callable[[], float] = time.monotonic,
        enabled: bool = True,
        ocr_first: bool = False,
    ) -> None:
        self._ensure_session = ensure_session
        self._collect_cc = collect_cc
        self._create_sampler = create_sampler
        self._load_region = load_region
        # 2026-11-11（区域校正）：扩展上报的 `videoRect` + 相对区域 → 当前屏幕区域。
        # 有它就优先用它；回 None（旧扩展/无视频框）再退回 `load_region()` 的绝对值。
        self._region_from_ctx = region_from_ctx
        # 最近一条心跳（区域校正用）：停播/暂停时也持续更新，重拖入账时按**当前**
        # 视频框换算相对比例，而不是上一次喂帧时的旧框。
        self._last_ctx: Mapping[str, Any] | None = None
        self._grab_band = grab_band
        self._screen_size = screen_size
        self._region_saver = region_saver
        self._clock = clock
        self.enabled = bool(enabled)
        # 2026-11-11（用户诉求）：OCR 先起手，CC 并行查。默认 False 保持既有
        # 「先查轨再采样」的测试口径不变；生产装配（build_default_scheduler）默认 True。
        self._ocr_first = bool(ocr_first)
        # v34 T3（票 #216）：手动扳机是**内存态**（不写 config）——中枢重启后
        # 新实例默认 False，第一次必须按一下才开始；`_armed_at` 是最近一次播放
        # 心跳（或置位）的时刻，空闲自解按它算。
        self.armed = False
        self._armed_at = 0.0
        self._lock = threading.Lock()
        self._state: _VideoState | None = None
        # 2026-11-10：播放状态去抖（见 `_idle_is_noise`）。同一页面会以 2 秒一条的
        # 节奏发来互相矛盾的心跳（多 frame / 预览 video / 失焦补发），记下「本段最后
        # 一次被判定在播的时刻」，用它抹平抖动。
        self._last_play_at = 0.0
        self._last_play_identity: tuple[str, int] | None = None
        # v34 T5（票 #218 / spec §C24）：事件冷却表在**实例**上，跨段共享 ——
        # 换视频/切 P 只丢 `_VideoState`，这里不清（否则同一个坏区域会被反复弹）。
        self._cooldowns: dict[str, float] = {}
        # 2026-11-11 ASR P2：无英文 CC 时通知 hub 排队转写；回 True = 已排队，
        # 本段置 `source_available=True` 让 10 秒无字源保底让位。
        self._on_cc_missing: Callable[[int, str, int], bool] | None = None
        self._events: list[dict[str, Any]] = []
        self._event_seq = 0

    # -- 心跳入口 ----------------------------------------------------------------

    def on_heartbeat(
        self, ctx: Mapping[str, Any], *, app_hint: str = ""
    ) -> list[str]:
        """一条心跳 = 一次调度 tick；返回动作账目（词表见模块头，测试断言口径）。

        任何内部故障都吞掉记一行日志 —— 心跳链路绝不 500、绝不中断整链（§B2c）。
        """
        try:
            with self._lock:
                return self._tick_locked(dict(ctx), app_hint=str(app_hint or ""))
        except Exception as exc:  # noqa: BLE001 - 调度器坏掉不许影响心跳响应
            log.warning("采集调度 tick 失败（%s: %s）", type(exc).__name__, exc)
            return []

    def _tick_locked(self, ctx: dict[str, Any], app_hint: str) -> list[str]:
        actions: list[str] = []
        if not self.enabled:
            if self._state is not None:
                self._drop_state(actions)  # 拨关瞬间把活动段 flush 干净
            return actions
        # v34 T3（票 #216）：未按开始键 = 零查轨零采样。判定放在开关之后、任何
        # 身份 / 会话 / 采样动作之前 —— 连 `bvid_and_page` 都不跑（不碰 href）。
        # 防御：armed 被旁路清掉时，残留段在这里立即 flush + 丢弃。
        if not self.armed:
            if self._state is not None:
                self._drop_state(actions)
            return actions
        identity = bvid_and_page(ctx.get("href"))
        # 区域校正：只记**本段页面**的最近心跳。后台 audible 标签的兜底心跳
        # 也会带 videoRect；若无条件覆盖，重拖时会把相对区域挂到别的视频框上。
        if self._state is None or identity == self._state.identity:
            self._last_ctx = ctx
        playing = identity is not None and playing_of(ctx)
        now = self._clock()
        # 空闲自解（spec §B13）：连续 10 分钟没有播放心跳 → 清零 + 一行日志，
        # 不弹窗。**先判间隔、再续期** —— 页面关一夜后的第一条播放心跳也必须
        # 触发自解，不能反过来给旧扳机「续命」（否则第二天不按 ^!7 就自动采）。
        if now - self._armed_at > ARM_IDLE_TTL_S:
            self.armed = False
            if self._state is not None:
                self._emit_event("stop", self._state)
                self._drop_state(actions)
            actions.append(A_ARM_IDLE)
            log.info(
                "采集调度：收句扳机空闲 %d 分钟没有播放心跳，已自动关闭",
                int(ARM_IDLE_TTL_S // 60),
            )
            return actions
        if playing:
            self._armed_at = now  # 播放心跳续期（空闲自解按「最近一次播放」算）
            # 只续本段的播放粘性：另一路视频的心跳不能把「本段还在播」一直续下去
            if self._state is None or identity == self._state.identity:
                self._last_play_at = now
                self._last_play_identity = identity
        if identity is None or not playing:
            # 2026-11-10：这条「未播放」心跳不一定真代表用户暂停了 —— 播放页会同时
            # 从多个 frame / 预览 video / 失焦补发处各发一条，`paused` 互相矛盾，
            # 两秒一条交替到达。原来任何一条都 `_suspend`，于是采样「开一秒停一秒」
            # （实测 15:03:53 启动采样，15:03:54 就被挂起 → 用户看到漏句 + 反应迟钝）。
            # 去抖后剩下的「真停播」才挂起；真暂停最多晚 PLAY_STICKY_S 秒。
            if self._idle_is_noise(ctx, playing, now):
                actions.append(A_IDLE)
                return actions
            self._suspend(actions)  # 停播 / 切页即停（flush；对象保留，回来续采）
            actions.append(A_IDLE)
            return actions
        # 「B站视频页 + 正在播放」成立（spec §C10 的触发门）
        # 2026-11-11：扩展改为可见页即报，双屏/后台 audible 可能同时有第二路在播。
        # 单段状态机不能被每 2 秒来回切段：本段还在播、新页面又没聚焦时，忽略它；
        # 用户真的切过去（focused=true）才换段。
        if (
            self._state is not None
            and self._state.identity != identity
            and not ctx.get("focused")
            and now - self._last_play_at < PLAY_STICKY_S
        ):
            actions.append(A_IDLE)
            return actions
        if self._state is not None and self._state.identity != identity:
            self._drop_state(actions)  # 换视频/切 P：旧段 flush + 丢弃
            actions.append(A_CHANGE)
            log.info("采集调度：切换视频段，旧段已 flush（待建新会话）")
        if self._state is None:
            self._state = _VideoState(
                identity=identity,
                href=str(ctx.get("href") or ""),
                title=str(ctx.get("title") or ""),
                now=now,
            )
        state = self._state
        state.href = str(ctx.get("href") or state.href)
        title = str(ctx.get("title") or "")
        if title:
            state.title = title
        # 1) 查轨（每段一次）：建会话 → collect_cc
        if state.session_id is None:
            state.session_id = self._safe_ensure_session(state.title, app_hint, state.href)
            if state.session_id is None:
                actions.append(A_NO_SESSION)
                return actions  # 会话没建起来：无处落库，下个心跳重试
        # 2026-11-11（用户诉求）：OCR 先行 —— 先起采样喂第一帧，查轨放后台并行。
        if self._ocr_first:
            self._tick_ocr_first(state, ctx, actions)
            return actions
        if not state.cc_checked:
            state.cc_checked = True
            actions.append(A_COLLECT)
            state.cc_done = self._run_cc(state)  # 英文轨落库成功 → 采样压制
            # v34 T5（spec §C24）：这一段开始收句（CC 直读或转采样都算）。
            self._emit_event("start", state)
        if state.cc_done:
            actions.append(A_IDLE)  # 英文 CC 整轨在库：采样不启动（§B8b）
            return actions
        # 2) 采样（CC 缺位/降级路径）
        if state.sampler is None:
            if state.sampler_failed:
                actions.append(A_IDLE)
                return actions  # 引擎建不起来：本段已记败，不逐 tick 抖引擎
            actions.append(A_START)
            log.info("采集调度：启动字幕带采样（会话 %d）", state.session_id)
            state.sampler = self._safe_create_sampler(state.session_id)
            if state.sampler is None:
                state.sampler_failed = True
                log.info("采集调度：OCR 不可用，本段不采样（换段自动重试）")
                actions.append(A_IDLE)
                return actions
            if state.cc_reason in ("no_track", "no_english_track", "error"):
                # 降级到画面识别（spec §C24 的五类事件之一）：告诉用户这条
                # 走的是字幕轨还是屏幕采样（同段只报一次）。
                self._emit_event("fallback", state)
        elif state.suspended:
            state.suspended = False
            actions.append(A_RESUME)
            log.info("采集调度：恢复播放，续采（会话 %d）", state.session_id)
        self._feed_tick(state, ctx, actions)
        return actions

    # -- OCR 先行（2026-11-11，用户诉求）------------------------------------------

    def _tick_ocr_first(
        self, state: _VideoState, ctx: Mapping[str, Any], actions: list[str]
    ) -> None:
        """先起采样喂第一帧，查轨放后台线程并行；CC 命中后再 flush + 停采样。

        为什么：查轨是网络请求（实测 ~0.65s，最坏 15s 超时），旧顺序会让第一帧
        OCR 一直等它。同句择优已现成（`store.BACKLOG_SOURCE_RANK` cc>asr>ocr），
        CC 晚到只会接管已落库的同句，不会重复。
        """
        if not state.cc_done and state.sampler is None and not state.sampler_failed:
            actions.append(A_START)
            log.info("采集调度：启动字幕带采样（会话 %d，OCR 先行）", state.session_id)
            state.sampler = self._safe_create_sampler(state.session_id)
            if state.sampler is None:
                state.sampler_failed = True
                log.info("采集调度：OCR 不可用，本段不采样（换段自动重试）")
        if state.sampler is not None:
            if state.suspended:
                state.suspended = False
                actions.append(A_RESUME)
                log.info("采集调度：恢复播放，续采（会话 %d）", state.session_id)
            self._feed_tick(state, ctx, actions)
        if not state.cc_checked and not state.cc_pending:
            state.cc_pending = True
            actions.append(A_COLLECT)
            self._start_cc_probe(state)
        if state.cc_pending and state.cc_result is not None:
            result = state.cc_result
            state.cc_result = None
            state.cc_pending = False
            state.cc_checked = True
            state.cc_done = self._apply_cc_result(state, result)
            # v34 T5（spec §C24）：这一段开始收句（CC 直读或转采样都算）。
            self._emit_event("start", state)
            if not state.cc_done:
                self._emit_event("fallback", state)
        if state.cc_checked and state.cc_done:
            if state.sampler is not None:
                self._flush_sampler(state, actions)
                state.sampler = None
                state.suspended = False
                state.dormant = False
            actions.append(A_IDLE)

    def _start_cc_probe(self, state: _VideoState) -> None:
        """后台查轨一次；结果只写 `state.cc_result`，由下一拍 tick 消费。"""
        bvid, page = state.identity
        session_id = state.session_id
        if session_id is None:
            state.cc_pending = False
            return

        def run() -> None:
            try:
                result = self._collect_cc(session_id, bvid, page)
            except Exception as exc:  # noqa: BLE001 - 来源故障静默降级（§B2c）
                log.warning("采集调度查轨失败（%s），按无轨降级采样", type(exc).__name__)
                result = {"reason": "error"}
            payload = dict(result) if isinstance(result, Mapping) else {"reason": "error"}
            with self._lock:
                state.cc_result = payload

        threading.Thread(target=run, name="capture-cc-probe", daemon=True).start()

    def _apply_cc_result(self, state: _VideoState, result: Any) -> bool:
        """查轨回包 → 段状态；英文轨命中返回 True（采样压制）。"""
        if not isinstance(result, Mapping):
            result = {"reason": "error"}
        reason = str(result.get("reason") or "")
        if reason:
            log.info("采集调度查轨跳过（reason=%s），转采样", reason)
            state.cc_reason = reason
            return False
        state.cc_reason = None
        added = int(result.get("added") or 0)
        # v34 T5（票 #218 / spec §C21）：CC 整轨也是「本段已收 N 句」的来源。
        if added > 0:
            state.added += added
            state.last_added_at = format_ts(datetime.now())
        log.info(
            "采集调度：英文 CC 轨已整轨落库（新增 %d 句），切到 CC 直读",
            added,
        )
        return True

    # -- 分支动作 ----------------------------------------------------------------

    def _idle_is_noise(self, ctx: Mapping[str, Any], playing: bool, now: float) -> bool:
        """这条「未播放」心跳该不该被当成真停播（2026-11-10 去抖）。

        回到用户看到的现象：「打开视频后服务反应很慢、老是漏句」。日志里采样是
        「开一秒、停一秒」：`15:03:53.307 启动字幕带采样` → `15:03:54.647 停播/切页，
        采样挂起` → 再恢复……每次挂起都 flush 一次，中间的帧就丢了。

        根因是**心跳不止一条**：B站 播放页会从多个 frame / 预览 video / 失焦补发处
        各发一条，`paused` 互相矛盾，两秒一条交替到达。原来只要有一条 playing=false
        就 `_suspend`，采样自然被反复打断。

        两条过滤（任一成立 → 这条心跳不算停播）：

        ① **不是本段的页面**：href 归一化后与活动段不同 —— 别的标签页在说话，与本段
           无关（真的换页时，新页会有一条 playing=true 的心跳走「切换视频段」）。
           2026-11-11：本段最近 `PLAY_STICKY_S` 秒内没有播放心跳时，这条不再当噪声 ——
           用户把同一个标签导航到非视频页后，旧页面不会再发播放心跳，若仍按「别的
           标签」忽略，活动段就永远不挂起、相位一直谎报 active。
        ② **播放粘性**：同页最近 `PLAY_STICKY_S` 秒内见过 playing=true —— 抖动被抹平；
           用户真暂停时，过了这个窗口照常挂起（最多晚 3 秒）。

        段还没建立（`_state is None`）时不去抖：arm 后的头几条心跳照旧走原判定，
        免得把「开收句时页面还没播」误当成在播。
        """
        state = self._state
        if state is None:
            return False
        href = str(ctx.get("href") or "")
        if href and state.href and context_slot_key(href) != context_slot_key(state.href):
            # 本段最近还在播 → 别的页面在说话；本段已 3 秒没有播放心跳 → 可能是
            # 同一标签导航走了，放行给 _suspend。
            return (
                self._last_play_identity == state.identity
                and now - self._last_play_at < PLAY_STICKY_S
            )
        if (
            not playing
            and self._last_play_identity == state.identity
            and now - self._last_play_at < PLAY_STICKY_S
        ):
            return True
        return False

    def _suspend(self, actions: list[str]) -> None:
        """停播/切页：活动采样 flush 一次并挂起（恢复播放续采，spec §C10）。"""
        state = self._state
        if state is not None and state.sampler is not None and not state.suspended:
            self._flush_sampler(state, actions)
            state.suspended = True
            # v34 T5（spec §C20）：暂停必须清 dormant —— 否则「先无区域、后暂停」
            # 会显示成「请重拖」，与「暂停不覆盖 idle」的目标相反。
            state.dormant = False
            # 无字源观测窗随停播重开：暂停期间不该把 10 秒耗掉，恢复播放再给一窗。
            # `sig_hit` **保留** —— 这段若已经见过一帧真字幕，就永久放行。
            state.window_start = 0.0
            state.frames_seen = 0
            actions.append(A_PAUSE)
            log.info(
                "采集调度：停播/切页，采样挂起（会话 %d，攒句已 flush）",
                state.session_id,
            )

    def _drop_state(self, actions: list[str]) -> None:
        """换视频/拨关/退出：flush（若有采样）+ 整段丢弃。"""
        state = self._state
        if state is None:
            return
        if state.sampler is not None:
            self._flush_sampler(state, actions)
        self._state = None

    def _run_cc(self, state: _VideoState) -> bool:
        """查轨一次（同步路径；`ocr_first=False` 的既有口径）。

        回包处理与 OCR 先行共用 `_apply_cc_result`，两套顺序不许各写一份。
        """
        bvid, page = state.identity
        assert state.session_id is not None  # 调用门：_tick_locked 先建会话才走到这
        try:
            result = self._collect_cc(state.session_id, bvid, page)
        except Exception as exc:  # noqa: BLE001 - 来源故障静默降级（§B2c）
            log.warning("采集调度查轨失败（%s），按无轨降级采样", type(exc).__name__)
            state.cc_reason = "error"
            return False
        return self._apply_cc_result(state, result)

    # -- 来源状态（挑词界面徽标的数据源，spec §B2c / §C11）------------------------

    def status_of(self, session_id: int) -> dict[str, Any]:
        """该会话的来源状态（纯读，不动状态机）。

        - `cc`：`ok`（英文轨已整轨落库）/ `no_english_track`（无轨或仅 ai-zh，
          cc_track 的 no_track 与 no_english_track 同样是「没有英文可用」）/
          `error`（网络/接口故障）/ `off`（当前没有段在跑）；
        - `ocr`：`active`（播放中采样）/ `idle`（停播挂起，回来自动续）/
          `dormant`（播放中但无区域，等用户拖一次 —— v34 T5 起不再谎报 active）/
          `redrag_needed`（签名连续失检，请用户重拖）/ `failed`（引擎建不起）/
          `off`（CC 在场被压制 / 段已结束）；
        - `enabled`：总开关（关 = 全链静默，cc/ocr 恒 off）；
        - `armed`：手动扳机（v34 T3）。未 armed 时 `ocr` 恒 off —— 判据在 enabled
          之后、`state is None` 之前（结构性保证，不靠段消失的顺序碰运气）。
        """
        with self._lock:
            if not self.enabled:
                return {"enabled": False, "armed": False, "cc": "off", "ocr": "off"}
            if not self.armed:
                return {"enabled": True, "armed": False, "cc": "off", "ocr": "off"}
            state = self._state
            if state is None or state.session_id != int(session_id):
                return {"enabled": True, "armed": True, "cc": "off", "ocr": "off"}
            cc, ocr = self._source_locked(state)
            return {"enabled": True, "armed": True, "cc": cc, "ocr": ocr}

    @staticmethod
    def _source_locked(state: _VideoState) -> tuple[str, str]:
        """段状态的来源枚举（`status_of` 与全局 `status` 共用的唯一映射）。"""
        if state.cc_done:
            cc = "ok"
        elif state.cc_reason in ("no_track", "no_english_track"):
            cc = "no_english_track"
        elif state.cc_reason == "error":
            cc = "error"
        else:
            cc = "off"
        ocr = "off"
        if state.sampler_failed:
            ocr = "failed"
        elif state.sampler is not None:
            if getattr(state.sampler, "needs_redrag", False):
                ocr = "redrag_needed"
            elif state.suspended:
                ocr = "idle"
            elif state.dormant:
                ocr = "dormant"
            else:
                ocr = "active"
        return cc, ocr

    def status(self, heartbeat_age_ms: int | None = None) -> dict[str, Any]:
        """全局相位出口（v34 T5 / 票 #218 / spec §C19–C21）：不需要先知道会话 id。

        `heartbeat_age_ms` 由 hub 从 `browser_context()` 传入（None = 从未收到
        心跳）：超过 `HEARTBEAT_STALE_MS` 即 `no_heartbeat`，段内状态不再冻结在
        active。回包字段即 `GET /api/capture/status` 的主体（`ok` 与会话标题由
        hub 补），外加 `events`（最近的关键事件，web 按 id 增量消费）。
        """
        with self._lock:
            state = self._state
            session_id: int | None = None
            added = 0
            last_added: str | None = None
            cc = "off"
            ocr = "off"
            cc_reading = False
            band_rect = ""
            band_auto = False
            if state is not None:
                session_id = state.session_id
                added = state.added
                last_added = state.last_added_at
                cc, ocr = self._source_locked(state)
                cc_reading = not state.cc_checked
                if state.region is not None:
                    band_rect = ",".join(str(int(v)) for v in state.region)
                    band_auto = bool(getattr(state, "band_auto", False))
            phase = phase_of(
                enabled=self.enabled,
                armed=self.armed,
                heartbeat_age_ms=heartbeat_age_ms,
                redrag=ocr == "redrag_needed",
                paused=state is None or bool(state.suspended),
                dormant=state is not None and state.dormant,
                failed=state is not None and state.sampler_failed,
                sampling=ocr == "active",
                cc_ok=cc == "ok",
                cc_missing=cc == "no_english_track",
                cc_error=cc == "error",
                cc_reading=cc_reading,
            )
            return {
                "enabled": bool(self.enabled),
                "armed": bool(self.armed),
                "phase": phase,
                "session_id": session_id,
                "last_added_at": last_added,
                "added_in_segment": int(added),
                "heartbeat_age_ms": heartbeat_age_ms,
                "cc": cc,
                "ocr": ocr,
                "band_rect": band_rect,
                "band_auto": band_auto,
                "events": [dict(event) for event in self._events],
            }

    def try_asr_for_no_source(self) -> bool:
        """无字源窗口到期且无 CC：排队 ASR 转写；成功则豁免 10 秒保底。

        hub 在组装 ping 前调用（先于 `no_source_active`）：排队成功的同一拍，
        `source_available=True`，于是 `cap_no_source` 保持 0，外壳不会停收句。
        只在「没有可读画面字源」时排队 —— 有画面字幕的视频继续走 OCR，
        不把音频无差别上传（隐私/费用口径）。
        """
        with self._lock:
            state = self._state
            if state is None or self._on_cc_missing is None or not self.armed:
                return False
            if state.asr_attempted or state.cc_done or state.session_id is None:
                return False
            cc, _ = self._source_locked(state)
            if cc not in ("no_english_track", "error"):
                return False
            if not no_source_window(
                now=self._clock(),
                window_start=state.window_start,
                sig_hit=state.sig_hit,
                frames_seen=state.frames_seen,
            ):
                return False
            state.asr_attempted = True
            try:
                queued = bool(
                    self._on_cc_missing(
                        int(state.session_id), state.identity[0], state.identity[1]
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 转写排队故障不许断采样
                log.warning("ASR 排队失败（%s），保底照旧", type(exc).__name__)
                queued = False
            if not queued:
                # 队列满/暂不可用不是「这条不能转写」：回滚尝试位，下一拍还能再排。
                state.asr_attempted = False
                return False
            state.source_available = True
            log.info(
                "采集调度：无画面字源 → ASR 转写接管（会话 %d）",
                state.session_id,
            )
            return True

    def no_source_active(self) -> bool:
        """本段是否已判「无画面字源」（hub ping 的 `cap_no_source` 唯一来源）。

        与 `status()` **刻意解耦**：hub 在 `capture_ping_fields()` 里单独取这个
        布尔，照 `cap_ended` 的先例走扁平 0/1，不进 `/api/capture/status` 回包
        （那边的字段集合有精确断言，也不值得为临时保底新增第 12 档相位）。

        纯读（除「每段只打一行 INFO」的记账位）、持锁；换段/挂起/关收句都会
        让 `_state` 或观测窗复位，判定自然回落。
        """
        with self._lock:
            state = self._state
            if state is None:
                return False
            cc, _ = self._source_locked(state)
            active = no_source_eligible(
                armed=self.armed,
                session_id=state.session_id,
                cc=cc,
                source_available=state.source_available,
                window_expired=no_source_window(
                    now=self._clock(),
                    window_start=state.window_start,
                    sig_hit=state.sig_hit,
                    frames_seen=state.frames_seen,
                ),
            )
            if active and not state.no_source_reported:
                state.no_source_reported = True
                log.info(
                    "采集调度：字幕带窗口 %d 帧 0 行可读文本，判无画面字源"
                    "（会话 %d）",
                    state.frames_seen,
                    state.session_id,
                )
            return active

    def _emit_event(self, kind: str, state: _VideoState | None = None) -> None:
        """关键事件入环（调用方须持锁）。

        两层去重（spec §C24）：同一段同一 kind 只报一次（`state.events_reported`）；
        同一 kind 在 `EVENT_COOLDOWN_S` 内只报一次（实例级 `_cooldowns`，跨段共享，
        `_drop_state` 不清）。被冷却挡下的事件**不进**段内集合 —— 冷却窗过后同一段
        还能补报。
        """
        if state is not None and kind in state.events_reported:
            return
        now = self._clock()
        last = self._cooldowns.get(kind)
        if last is not None and now - last < EVENT_COOLDOWN_S:
            return
        if state is not None:
            state.events_reported.add(kind)
        self._cooldowns[kind] = now
        self._event_seq += 1
        self._events.append(
            {
                "id": self._event_seq,
                "kind": kind,
                "session_id": state.session_id if state is not None else None,
                "at": format_ts(datetime.now()),
            }
        )
        if len(self._events) > EVENT_KEEP:
            del self._events[:-EVENT_KEEP]

    def _feed_tick(self, state: _VideoState, ctx: Mapping[str, Any], actions: list[str]) -> None:
        """播放中的一帧：取区域 → 抓带 → feed → 周期 flush → 重拖信号。"""
        # 无字源观测窗：每进一次喂帧 tick 算一帧，**包括抓带失败直接 SKIP 的那一支**
        # （2026-11-10 真机：kernel.log 零 OCR 请求，空转的正是这一支）。
        # 起窗放在首个喂帧 tick，而不是建段 —— 查轨/建采样器的耗时不该吃掉 10 秒。
        if state.frames_seen <= 0 or state.window_start <= 0:
            state.window_start = self._clock()
        state.frames_seen += 1
        video_rect = video_rect_from_context(ctx)
        # 区域校正（2026-11-11）：视频框变了（页面滚动/影院模式/挪窗）就按相对区域
        # 或默认带重算；没有 `region_from_ctx`（旧装配/测试）时退回首次加载语义。
        rect_changed = (
            self._region_from_ctx is not None
            and video_rect is not None
            and video_rect != state.video_rect
        )
        if state.region is None or rect_changed:
            fresh = self._safe_load_region(ctx)
            if fresh is None and state.region is None:
                # 第一档落空（无历史区域）：休眠待重拖——每段 INFO 提示一次，
                # 之后逐 tick 降 DEBUG（别刷屏）。
                state.dormant = True
                state.video_rect = video_rect
                if not state.dormant_reported:
                    state.dormant_reported = True
                    log.info(
                        "采集调度：没有历史字幕带区域，采样休眠（会话 %d，请拖一次区域）",
                        state.session_id,
                    )
                else:
                    log.debug("采集调度：仍无区域，采样继续休眠（会话 %d）", state.session_id)
                # 同一段只报一次（事件冷却表在实例上，跨段的 5 分钟冷却另算）
                self._emit_event("redrag", state)
                actions.append(A_DORMANT)
                return
            if fresh is not None:
                first_load = state.region is None
                state.region = fresh
                # 区域加载/重投影成功：休眠结束；只有首次自动加载才标记「自动框」
                # （用户拖过的框在滚动重投影后仍算手动，不该再被当成自动框）。
                state.dormant = False
                if first_load:
                    state.band_auto = True
        state.video_rect = video_rect
        band = self._safe_grab_band(state.region)
        if band is None:
            actions.append(A_SKIP)
            return
        t = ctx.get("currentTime")
        try:
            seconds = float(t)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            seconds = 0.0
        try:
            report = state.sampler.feed(band, seconds)
        except Exception as exc:  # noqa: BLE001 - 采样故障静默降级（§B2c）
            log.warning("采样 feed 失败（%s），本帧跳过", type(exc).__name__)
            actions.append(A_SKIP)
            return
        # 判「有没有可读字幕」看 OCR 是否真的识别出拉丁行（`report.lines`），
        # 不看 `sig_ok`：后者是墨量占比签名，播放器控件白字、亮场景都会误命中
        # （2026-11-11 真机：带子压在控件上，ink 签名恒过 → 永不判无字源）。
        # 测试假件可能用 dict，两种都认。
        lines = getattr(report, "lines", 0)
        if not lines and isinstance(report, Mapping):
            lines = report.get("lines") or 0
        try:
            lines = int(lines or 0)
        except (TypeError, ValueError):
            lines = 0
        if lines > 0:
            state.sig_hit = True
        actions.append(A_FEED)
        if state.sampler.needs_redrag:
            if not state.redrag_reported:
                state.redrag_reported = True
                actions.append(A_NEEDS_REDRAG)  # 第二档升级信号（hub 日志可见，T2 已 log）
            # v34 T5：区域失效也是「需要重拖」（与无区域同属一类；段内只报一次）
            self._emit_event("redrag", state)
        elif state.redrag_reported:
            # 粘滞 redrag 修复：签名恢复（采样器撤回标志）后解除已报状态——
            # 若之后再度连续失检，要能**重新报**（撤回不是一次性熔断）。
            state.redrag_reported = False
        now = self._clock()
        if now - state.last_flush >= FLUSH_PERIOD_S:
            self._flush_sampler(state, actions)

    def _flush_sampler(self, state: _VideoState, actions: list[str]) -> None:
        if state.sampler is None:
            return
        try:
            result = state.sampler.flush()
        except Exception as exc:  # noqa: BLE001 - flush 失败不断链（投票组随下轮重flush）
            log.warning("采样 flush 失败（%s）", type(exc).__name__)
            return
        # v34 T5（票 #218 / R3 §2.1 #14/#15）：接住 flush 回包的 added —— 本段
        # 累计句数与「最近一次落库」是状态条回答「是不是卡住了」的两个数。
        added = 0
        if isinstance(result, Mapping):
            try:
                added = int(result.get("added") or 0)
            except (TypeError, ValueError):
                added = 0
        if added > 0:
            state.added += added
            state.last_added_at = format_ts(datetime.now())
        state.last_flush = self._clock()
        actions.append(A_FLUSH)

    # -- 注入件的静默兜底（§B2c：单一来源故障只记一行日志，绝不炸心跳链）-----------

    def _guard(self, action: str, fn: Callable[..., Any], *args: Any) -> Any:
        """注入件调用的共用兜底：抛错记日志回 None，注入点签名不变。"""
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001
            log.warning("采集调度%s失败（%s）", action, type(exc).__name__)
            return None

    def _safe_ensure_session(self, title: str, app_hint: str, href: str) -> int | None:
        return self._guard("建会话", self._ensure_session, title, app_hint, href)

    def _safe_create_sampler(self, session_id: int) -> Any | None:
        return self._guard("建采样器", self._create_sampler, session_id)

    def _safe_load_region(
        self, ctx: Mapping[str, Any] | None = None
    ) -> tuple[int, int, int, int] | None:
        if self._region_from_ctx is not None and ctx is not None:
            region = self._guard("读区域", self._region_from_ctx, ctx)
            if region is not None:
                return region
        return self._guard("读区域", self._load_region)

    def _safe_grab_band(self, region: tuple[int, int, int, int]) -> Any | None:
        return self._guard("抓字幕带", self._grab_band, region)

    # -- 手动扳机（v34 T3 / spec §B11–B13；热键 → RPC capture_arm 的落地）----------

    def set_armed(self, on: bool) -> dict[str, Any]:
        """绝对置位/清零（总开关联动入口；RPC 的 op 语义见 `capture_arm`）。

        置位不校验视频页/总开关 —— 那是 RPC 语义（`capture_arm`）的事；
        hub 的 `reload_switches` 把「视频收句」拨成 0 时用 `set_armed(False)`
        顺手清零，避免再拨回来时「自己开始」。清零持锁立即 flush + 丢弃当前段。
        """
        with self._lock:
            return self._set_armed_locked(bool(on))

    def capture_arm(self, op: str, *, video_page: bool) -> dict[str, Any]:
        """`{"cmd":"capture_arm","op":"toggle"|"on"|"off"}` 的调度器语义。

        - `on`：仅当当前浏览器上下文是 B站视频页（`video_page` 由 hub 判定）且
          总开关开着才置位；**不要求正在播放**（可以先暂停再按）；
        - `toggle`：已置位 → 关（等同 off）；未置位 → 同 on；
        - `off`：无条件允许（切走标签页后也必须能停），持锁立即 flush + 丢弃当前段。

        回包 `{ok, armed, reason}`；失败时 `ok=false` 且 `reason` 是
        `"no_video"` / `"disabled"`，`armed` 保持关（不猜、不半开）。
        `op` 非法回 bad request（hub 先校验，这里兜底）。
        """
        with self._lock:
            if op not in ("toggle", "on", "off"):
                return {"ok": False, "error": "bad request"}
            if op == "off" or (op == "toggle" and self.armed):
                return self._set_armed_locked(False)
            if not self.enabled:
                return {"ok": False, "armed": bool(self.armed), "reason": "disabled"}
            if not video_page:
                return {"ok": False, "armed": bool(self.armed), "reason": "no_video"}
            return self._set_armed_locked(True)

    def _set_armed_locked(self, on: bool) -> dict[str, Any]:
        if on:
            if not self.armed:
                log.info("采集调度：收句扳机已开启（等播放心跳）")
            self.armed = True
            self._armed_at = self._clock()
            return {"ok": True, "armed": True, "reason": ""}
        was = self.armed
        had_segment = self._state is not None
        self.armed = False
        if was and self._state is not None:
            self._emit_event("stop", self._state)
        actions: list[str] = []
        self._drop_state(actions)
        if was:
            log.info(
                "采集调度：收句扳机已关闭%s",
                "（当前段已 flush）" if had_segment else "",
            )
        return {"ok": True, "armed": False, "reason": ""}

    # -- 重拖（第三档函数入口；热键/手势把矩形送进来）-----------------------------

    def redrag(self, region: Any) -> dict[str, Any]:
        """用户重拖了字幕带区域：持久化 + 重置签名失检（T2 `set_region` 的接线点）。

        无活动段时也照常持久化（下个分段的第一档就能读到新区域）。
        """
        with self._lock:
            screen = self._screen_size()
            if screen is None:
                return {"ok": False, "reason": "no_screen"}
            rect = _normalize_region(region)
            if rect is None:
                return {"ok": False, "reason": "bad_region"}
            width, height = int(screen[0]), int(screen[1])
            # 用**最近一条心跳**的视频框刷新相对坐标（暂停/停播时喂帧不跑，
            # last_ctx 不能停留在上一次播放的旧框）。
            if self._region_from_ctx is not None and self._last_ctx is not None:
                self._guard("刷新视频框", self._region_from_ctx, self._last_ctx)
            state = self._state
            try:
                if state is not None and state.sampler is not None:
                    normalized = state.sampler.set_region(rect, screen_w=width, screen_h=height)
                    state.region = tuple(normalized)
                    state.redrag_reported = False
                    # 采样器已写绝对区域；再走一次 saver 让生产装配同步**相对区域**
                    # （绝对键幂等重写），换布局/滚动时才能按视频框重投影。
                    self._region_saver(tuple(normalized), width, height)
                else:
                    normalized = self._region_saver(rect, width, height)
                # v34 T5（spec §C20）：重拖成功即结束休眠（region 已经有着落）
                if state is not None:
                    state.dormant = False
                    state.band_auto = False
            except Exception as exc:  # noqa: BLE001
                log.warning("重拖区域入账失败（%s）", type(exc).__name__)
                return {"ok": False, "reason": "error"}
            log.info("字幕带区域已重拖（%dx%d）", width, height)
            return {"ok": True, "region": list(normalized)}

    # -- ASR P2 接线（2026-11-11）------------------------------------------------

    def set_cc_missing_hook(self, hook: Any) -> None:
        """无英文 CC 时通知 hub 排队转写；回 True = 已排队，本段豁免无字源保底。"""
        self._on_cc_missing = hook

    def set_source_available(self, session_id: int, available: bool) -> None:
        """ASR 失败时撤回豁免位（只有当前段吃；找不到段静默）。"""
        with self._lock:
            state = self._state
            if state is not None and state.session_id == int(session_id):
                state.source_available = bool(available)

    def note_added(self, session_id: int, added: int) -> None:
        """ASR 落池后把句数记进段统计（`cap_added` / 最近落库时间）。

        纯 ASR 会话没有 OCR flush；不记的话状态条永远显示「本段 0 句」，和
        `cap_asr_added` 对不上，用户会以为卡住了。
        """
        if int(added) <= 0:
            return
        with self._lock:
            state = self._state
            if state is not None and state.session_id == int(session_id):
                state.added += int(added)
                state.last_added_at = format_ts(datetime.now())

    # -- 退出 --------------------------------------------------------------------

    def shutdown(self) -> None:
        """hub 退出：活动段 flush 后丢弃（投票组不落库就丢等于白采一段）。"""
        try:
            with self._lock:
                actions: list[str] = []
                self._drop_state(actions)
        except Exception as exc:  # noqa: BLE001 - 退出路径不许再抛
            log.warning("采集调度退出 flush 失败（%s）", type(exc).__name__)


def _normalize_region(region: Any) -> tuple[int, int, int, int] | None:
    """重拖入参 → 四个正整数；任何不规范输入回 None（绝不猜）。"""
    if isinstance(region, (str, bytes)) or not hasattr(region, "__len__"):
        return None
    values = list(region)
    if len(values) != 4:
        return None
    try:
        numbers = tuple(int(v) for v in values)
    except (TypeError, ValueError):
        return None
    if min(numbers) < 1:
        return None
    return numbers  # type: ignore[return-value]


def ensure_video_session(store: Any, title: str, app: str, href: str) -> int | None:
    """调度器建会话的默认实现：归户/新建 + 把视频 href 落进 `sessions.url`。

    纯观看链路没有任何捕获写 url 的机会（capture 的会话上下文只在取词时跑），
    挑词界面的「▶ 回到 B站」与时间跳转靠这里（spec 用户故事 16）。会话照常建，
    url 写失败（非法形态等）只记日志不回滚 —— 跳转链接是增强能力，不是收录门槛。
    """
    from datetime import datetime

    plan = store.session_plan(str(app or ""), str(title or ""))
    session_id = int(store.apply_session_plan(plan, app=str(app or ""), now=datetime.now()))
    url = str(href or "").strip()
    if url:
        try:
            store.update_session(session_id, {"url": url})
        except Exception as exc:  # noqa: BLE001
            log.warning("会话跳回链接写入跳过（%s）", type(exc).__name__)
    return session_id


# --- hub 侧默认装配（生产适配器；测试用假件直接构造 CaptureScheduler）---------------


def build_default_scheduler(
    store: Any,
    config_path: Any,
    *,
    enabled: bool = True,
    band_ocr: Any | None = None,
    ocr_first: bool = True,
) -> CaptureScheduler:
    """把调度器接到真实零件上：T1 查轨 / T2 采样器 / config 区域 / GDI 抓带。

    引擎懒加载且**跨分段复用**（RapidOCR 模型加载很贵，不能逐段重建）。

    v33 P0（票 #211 首发崩溃修复）：引擎一律是 **worker 子进程代理**
    （`ocr_sampler.OcrWorkerProxy`）——hub 进程已加载 winrt/comtypes，同进程
    起 onnxruntime 会原生段错误（T4 在案）。hub 会把共享代理从 `band_ocr=`
    递进来（调度器采样与 reocr 端点共用同一个子进程）；直接调本工厂（测试/
    工具）时现场建一个。`create_band_ocr`（进程内引擎）在 hub 里禁用。
    """
    import configparser  # 局部导入：调度器模块不背上 hub 的导入链

    import cc_track
    import kernel
    import ocr_sampler
    from settings import SECTION

    config_path = str(config_path)
    engine_box: dict[str, Any] = {"engine": band_ocr}

    def _section_config() -> dict[str, str]:
        parser = configparser.ConfigParser(interpolation=None)
        if not parser.read(config_path, encoding="utf-8") or not parser.has_section(SECTION):
            return {}
        return dict(parser[SECTION])

    def ensure_session(title: str, app: str, href: str) -> int | None:
        return ensure_video_session(store, title, app, href)

    def build_space_fixer() -> Callable[[str], str] | None:
        """#221：OCR 丢空格修复（与截图取词共用 `ocr_correct` 总闸与词表）。"""
        try:
            if kernel.config_int(_section_config(), "ocr_correct", 1) == 0:
                return None
            known, common = kernel.correction_wordlists()
            if not known:
                return None
            import corrections  # 局部导入：纯函数模块，不扩大调度器导入链

            return lambda text: corrections.restore_spaces(text, known, common)
        except Exception as exc:  # noqa: BLE001 - 修复器坏掉不许影响采样
            log.warning("字幕粘连空格修复不可用（%s）", type(exc).__name__)
            return None

    space_fixer = build_space_fixer()

    def resolve_page_cid(bvid: str, page: int) -> int | None:
        """多 P 的 cid（spec §B7）：`view.data.pages[k].cid`；解析不出 → None。

        只在 page > 1 时走（P1 直接让 `cc_track.collect` 用 `view.data.cid`，省一次
        view 调用）。
        """
        try:
            query = urllib.parse.urlencode({"bvid": bvid})
            view = json.loads(cc_track.default_fetch(f"{cc_track.VIEW_API}?{query}").decode("utf-8"))
            if not isinstance(view, Mapping) or view.get("code") != 0:
                return None
            pages = (view.get("data") or {}).get("pages") or []
            for item in pages:
                if not isinstance(item, Mapping):
                    continue
                if int(item.get("page") or 0) == page and isinstance(item.get("cid"), int):
                    return int(item["cid"])
        except Exception:  # noqa: BLE001 - 与查轨同一降级口径
            return None
        return None

    def collect_cc(session_id: int, bvid: str, page: int) -> dict[str, Any]:
        cid: int | None = None
        if page > 1:
            cid = resolve_page_cid(bvid, page)
            if cid is None:
                return {
                    "added": 0,
                    "total": store.subtitle_pending_count(session_id),
                    "reason": "error",
                }
        return dict(cc_track.collect(store, session_id, bvid, cid=cid))

    def create_sampler(session_id: int) -> Any | None:
        if engine_box["engine"] is None:
            engine_box["engine"] = ocr_sampler.create_band_ocr_worker()
        engine = engine_box["engine"]
        if engine is None:
            return None
        return ocr_sampler.SubtitleBandSampler(
            store,
            session_id,
            ocr=engine,
            config_path=config_path,
            space_fixer=space_fixer,
            # D7 底图目录统一：底图跟 DB 走（db 旁目录），hub 的 serve / 重识别 /
            # image_ok / 清理对账全都按这个根找 —— 从 worktree 跑、config 的
            # db_path 指向别处时也不再「图写了却读不到」。不 import hub，
            # 路径直接从已有 store 实例拿。
            image_dir=store.db_path.parent / "subtitle_bands",
        )

    def screen_size() -> tuple[int, int] | None:
        try:
            _x, _y, width, height = kernel.virtual_screen_bounds()
            return int(width), int(height)
        except Exception:  # noqa: BLE001
            return None

    def load_region() -> tuple[int, int, int, int] | None:
        screen = screen_size()
        if screen is None:
            return None
        region = ocr_sampler.load_last_region(_section_config(), screen[0], screen[1])
        if region is None:
            return None
        # 2026-11-11：自动复用区域纵向扩 15%（包住两行字幕）；只读时扩、不写回。
        return ocr_sampler.pad_region(region, screen[0], screen[1])

    # 区域校正（2026-11-11）：最近一次心跳里的视频内容框。重拖入账时用它把绝对
    # 区域换算成相对比例；每次喂帧先由 `region_from_ctx` 更新。
    last_ctx: dict[str, tuple[int, int, int, int] | None] = {"video_rect": None}

    def region_from_ctx(ctx: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
        """视频内容框 + 相对区域/默认带 → 当前屏幕区域；无框 → None（走绝对值）。"""
        screen = screen_size()
        if screen is None:
            return None
        rect = ocr_sampler.video_rect_from_context(ctx)
        last_ctx["video_rect"] = rect
        if rect is None:
            return None
        rel = ocr_sampler.load_last_relative(_section_config(), screen[0], screen[1])
        region = (
            ocr_sampler.region_from_relative(rel, rect)
            if rel is not None
            else ocr_sampler.auto_band_from_video(rect)
        )
        return ocr_sampler.pad_region(region, screen[0], screen[1])

    def grab_band(region: tuple[int, int, int, int]) -> Any:
        return kernel.grab_screen(tuple(region))

    def region_saver(
        region: tuple[int, int, int, int], screen_w: int, screen_h: int
    ) -> tuple[int, int, int, int]:
        normalized = ocr_sampler.save_last_region(config_path, screen_w, screen_h, region)
        video_rect = last_ctx["video_rect"]
        if video_rect is not None:
            rel = ocr_sampler.region_to_relative(normalized, video_rect)
            if rel is not None:
                try:
                    ocr_sampler.save_last_relative(config_path, screen_w, screen_h, rel)
                except Exception as exc:  # noqa: BLE001 - 相对键写失败不许毁掉重拖
                    log.warning("相对区域写入失败（%s）", type(exc).__name__)
        return normalized

    return CaptureScheduler(
        ensure_session=ensure_session,
        collect_cc=collect_cc,
        create_sampler=create_sampler,
        load_region=load_region,
        grab_band=grab_band,
        screen_size=screen_size,
        region_saver=region_saver,
        region_from_ctx=region_from_ctx,
        enabled=enabled,
        ocr_first=ocr_first,
    )
