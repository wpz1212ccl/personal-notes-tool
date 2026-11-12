"""CC 字幕轨直读（v33 T1，票 #207 / spec §B7）：查轨 → 整轨下载 → 待选句入库。

事实来源（端点选型 / JSON 形状 / 坑点，2026-11-07 免登录实测）：
`docs/research/v33-capture-routes/2026-11-07-CC字幕轨直读端到端验证.md`

- 查轨端点是 **`x/v2/dm/view`**（弹幕接口）：`x/player/v2` 对游客恒回空
  `subtitles[]`（假阴性），绝不能用（研究 §5.1）；
- 调用序（研究 §6）：`view?bvid=` → `dm/view?aid=&oid=<cid>&type=1` → 字幕 JSON；
  轨按**分 P 粒度**（每个 cid 一套轨），多 P 用 `view.data.pages[].cid` 逐 P 查；
- 选轨只认**人工轨**（type=0）且 `lan` 以 en 开头（en/en-US/en-GB）；AI 轨
  （type=1，如 ai-zh 中文翻译）质量低、不收英文词，无英文人工轨 → 静默跳过
  （零入库、不报错、不阻塞调用方，spec §B7「没有就静默走采样」）；
- 字幕 JSON **裸抓**即可（无任何 header，与 DASH 音频流的 Referer+UA 不同）；
  URL 可能是 `http://` 或 `//` 开头，一律升级 https（研究 §5.3/§5.7）；
- 一行 cue 的 `content` 内嵌 `\n`（多行文本），入库前清洗成单行（研究 §5.8）；
- 只收拉丁行（O1 契约：中英双语轨的中文行不收，票 #207 AC6）。

网络层可注入：所有 HTTP 都走 `fetch(url) -> bytes`（默认 urllib 裸抓），测试传
假 fetch 全程离线。日志纪律（store §10 同款）：只记数量与原因码，不记句文本。
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from store import Store, is_latin_line, one_line

log = logging.getLogger("hub.cc_track")

# 免登录端点（研究 §2：全程无 cookie；dm/view 的 oid = 分 P 的 cid，type=1=普通视频）
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
DM_VIEW_API = "https://api.bilibili.com/x/v2/dm/view"
DEFAULT_TIMEOUT_S = 15
# API 请求带 UA（照取证探针 probe_cc.py 的手法：无 cookie，只有 UA）；
# 字幕 JSON 裸抓即 200（研究 §5.3「带 UA+Referer 也 200（无害）」），统一带 UA 最简。
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# collect 的结果 `reason`：None=成功落库；其余为静默跳过的原因码（调用方据此走
# 下一优先级来源，spec §B2c 降级）。
REASON_NO_TRACK = "no_track"  # dm/view 无任何轨
REASON_NO_ENGLISH = "no_english_track"  # 有轨但没有英文人工轨（仅 ai-zh / zh 轨）
REASON_ERROR = "error"  # 网络/接口/解析失败（含 API code != 0）

# 生产网络层：裸抓（不带任何 header —— 研究 §5.3 实测字幕 JSON 无需 header）。
Fetch = Callable[[str], bytes]


def default_fetch(url: str) -> bytes:
    """默认网络层：urllib + UA、无 cookie（免登录链路）。测试经 `fetch=` 注入假网络层。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_S) as response:
        return response.read()


def https_url(url: str) -> str:
    """字幕 JSON 的 URL 统一升级 https（研究 §5.7：http:// 与 // 都实测 200，升级无害）。

    query 原样保留 —— AI 轨的 `auth_key` 限时签名在里面（研究 §5.4），丢了就 403。
    """
    text = (url or "").strip()
    if text.startswith("//"):
        return f"https:{text}"
    if text.startswith("http://"):
        return f"https://{text[7:]}"
    return text


def pick_english_track(subtitles: Any) -> dict[str, Any] | None:
    """从 `dm/view` 的 `subtitles[]` 里挑英文人工轨；没有就 None（纯函数）。

    只认 `type=0`（人工上传）且 `lan` 以 en 开头（en/en-US/en-GB，大小写不敏感）；
    AI 轨（type=1）一概不认 —— 研究实测 AI 轨有「哎呦」这类语气误识别，质量明显低。
    多条英文轨取第一条（dm/view 无优先级语义，先见先得）。
    """
    if not isinstance(subtitles, list):
        return None
    for track in subtitles:
        if not isinstance(track, Mapping):
            continue
        if track.get("type") != 0:
            continue
        lan = str(track.get("lan") or "").strip().lower()
        if lan.startswith("en"):
            return dict(track)
    return None


def track_lines(track_json: Any) -> list[dict[str, Any]]:
    """轨 JSON → 待选句行列表（纯函数）：清洗 → 拉丁过滤 → 时间点。

    轨 JSON 形状（研究 §3）：顶层 `{body: [...]}`（样式键可选，解析器必须容忍）；
    行键 `content`（可内嵌 `\n`）/ `from` / `to`（秒，浮点）。坏行（无时间点、
    空文本、非拉丁行）跳过，不打断整轨。
    """
    if not isinstance(track_json, Mapping):
        return []
    body = track_json.get("body")
    if not isinstance(body, list):
        return []
    rows: list[dict[str, Any]] = []
    for cue in body:
        if not isinstance(cue, Mapping):
            continue
        # content 清洗：内嵌 \n → 空格、折叠空白（与 store.one_line 同口径）
        text = one_line(cue.get("content"))
        try:
            from_s = float(cue["from"])  # type: ignore[arg-type]
            to_s = float(cue["to"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            continue
        if not text or not is_latin_line(text):
            continue
        rows.append(
            {
                "text": text,
                "source": "cc",
                "from_s": from_s,
                "to_s": to_s,
                # CC 整轨是人工上传的精确文本：无 OCR 置信度、不可疑、无底图
                "score": None,
                "suspicious": 0,
                "image_path": None,
            }
        )
    return rows


def collect(
    store: Store,
    session_id: int,
    bvid: str,
    *,
    cid: int | None = None,
    fetch: Fetch | None = None,
) -> dict[str, Any]:
    """查轨 → 整轨下载 → 待选句入库（票 #207 的 CC 分支组合入口）。

    `cid` 缺省用 `view.data.cid`（P1）；多 P 会话由调用方（T5 编排）传当前分 P 的
    cid（浏览器上下文里有）。任何失败（网络断 / 接口变 / code != 0 / JSON 坏）
    都**静默**返回 `reason`，绝不抛到调用方 —— 单一来源故障不许中断整链（§B2c）。

    返回 `{"added": n, "total": m, "reason": None | no_track | no_english_track | error}`；
    `total` 是该会话入库后的待选句总数（未入库时为 0）。
    """
    do_fetch: Fetch = fetch if fetch is not None else default_fetch
    total = store.subtitle_pending_count(session_id)
    try:
        # 第 1 步：view 拿 aid / cid（研究 §6 步骤 1）
        view_query = urllib.parse.urlencode({"bvid": bvid})
        view = json.loads(do_fetch(f"{VIEW_API}?{view_query}").decode("utf-8"))
        if not isinstance(view, Mapping) or view.get("code") != 0:
            log.warning("cc_track view 接口 code 非 0，跳过查轨")
            return {"added": 0, "total": total, "reason": REASON_ERROR}
        view_data = view.get("data") or {}
        aid = view_data.get("aid")
        oid = int(cid) if cid is not None else view_data.get("cid")
        if not isinstance(aid, int) or not isinstance(oid, int):
            log.warning("cc_track view 响应缺 aid/cid，跳过查轨")
            return {"added": 0, "total": total, "reason": REASON_ERROR}
        # 第 2 步：dm/view 查轨（研究 §6 步骤 2；oid=分 P 的 cid）
        dm_query = urllib.parse.urlencode({"aid": aid, "oid": oid, "type": 1})
        dm_view = json.loads(do_fetch(f"{DM_VIEW_API}?{dm_query}").decode("utf-8"))
        if not isinstance(dm_view, Mapping) or dm_view.get("code") != 0:
            log.warning("cc_track dm/view 接口 code 非 0，跳过查轨")
            return {"added": 0, "total": total, "reason": REASON_ERROR}
        subtitle = (dm_view.get("data") or {}).get("subtitle") or {}
        subtitles = subtitle.get("subtitles") or []
        # 第 3 步：选轨 —— 只认英文人工轨（研究 §6 步骤 3）
        track = pick_english_track(subtitles)
        if track is None:
            reason = REASON_NO_TRACK if not subtitles else REASON_NO_ENGLISH
            log.info("cc_track 无英文人工轨（轨数 %d），静默跳过", len(subtitles))
            return {"added": 0, "total": total, "reason": reason}
        # 第 4 步：整轨下载（紧跟查轨：auth_key 限时签名，研究 §5.4）
        url = https_url(str(track.get("subtitle_url") or ""))
        if not url.startswith("https://"):
            log.warning("cc_track 轨 URL 缺失或形态不对，跳过下载")
            return {"added": 0, "total": total, "reason": REASON_ERROR}
        track_json = json.loads(do_fetch(url).decode("utf-8"))
        # 第 5 步：清洗 + 拉丁过滤 + 入库（唯一键去重在 store 层）
        result = store.add_subtitle_lines(session_id, track_lines(track_json))
        log.info(
            "cc_track 落库完成（新增 %d 重复 %d 无效 %d）",
            result["added"],
            result["duplicates"],
            result["invalid"],
        )
        return {
            "added": result["added"],
            "total": result["total"],
            "reason": None,
        }
    except Exception as exc:  # noqa: BLE001 - 来源故障静默降级（spec §B2c）
        log.warning("cc_track 查轨失败（%s），静默跳过", type(exc).__name__)
        return {"added": 0, "total": total, "reason": REASON_ERROR}
