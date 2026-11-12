"""v35 底图自动清理（票 #226 / #235，spec `docs/specs/v35-band-image-retention.md`）。

底图的清理要同时看**两侧**：`subtitle_backlog.image_path` 的引用行 + `subtitle_bands/`
目录里的文件。所以本模块独立于 `maintenance.py`（那边是「只吃目录、纯函数」），
但沿用同一套纪律：

- 只做「选择与执行」，不碰 Store 实现细节 —— 引用行与置空动作都走**窄回调**；
- 纯函数 + 可注入 `now`，测试只造临时目录与假回调（不依赖真实时间 / 真实数据目录）；
- 只删白名单文件名（`^s\\d+_f\\d+\\.png$`）且 `resolve()` 后仍在底图目录内的文件；
- **先置空 DB 再 unlink**：崩溃最多留下孤儿文件，下一轮能扫掉（spec 规则 6）。

票 #226 落孤儿清扫（24h 年龄门，避开「文件已落盘、行还没插」的在途窗口）与缺图对账；
#235 在同一函数里追加已挑 TTL 与容量兜底。首版 `dry_run=True`：一个文件都不删、
DB 不变，只在结果与日志里报「预计可删 N 张 / X MB」。

日志纪律（spec 规则 8）：每轮只写**一行汇总**（删 N 张 / 释放 X MB / 置空 M 行 /
保留 K 张），不逐文件刷屏；非白名单文件只汇总一条 warning。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sessions import parse_ts

log = logging.getLogger("hub")

HOUR_S = 3600.0
DAY_S = 86400.0
DEFAULT_ORPHAN_MIN_AGE_S = 24 * HOUR_S
DEFAULT_TTL_DAYS = 30
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
TARGET_RATIO = 0.8  # 容量兜底删到上限的 80%，留一段免得下一轮立刻又触发
# 只认采样器落盘的文件名（`ocr_sampler._default_save_image`）：`s<会话>_f<帧>.png`。
_BAND_NAME = re.compile(r"^s\d+_f\d+\.png$")


@dataclass(frozen=True)
class BandCleanupResult:
    """一轮底图清理的结果。

    `deleted_files` / `freed_bytes` / `cleared_rows` 是**实际**发生的；
    `planned_*` 是**本轮打算**做的（dry-run 下 = 预计可删数，非 dry-run 下与实际
    通常一致 —— 分开记是为了日志能如实说「预计」而不冒充「已删」）。
    """

    deleted_files: int = 0
    freed_bytes: int = 0
    cleared_rows: int = 0
    kept_files: int = 0
    dry_run: bool = False
    planned_files: int = 0
    planned_bytes: int = 0
    planned_cleared: int = 0


def _scan(root: Path) -> tuple[dict[str, tuple[Path, int, float]], int]:
    """扫底图目录 → ({白名单文件名: (路径, 字节, mtime)}, 非白名单/越界文件数)。

    目录不存在 / 读不到一律当空（不抛）；非白名单与 `resolve()` 后跑出目录的一律
    不删（spec 规则 1），只返回计数给调用方汇总成一条 warning。mtime 供容量兜底
    「按旧到新淘汰」排序用。
    """
    try:
        entries = list(root.iterdir())
    except OSError:
        return {}, 0
    band_files: dict[str, tuple[Path, int, float]] = {}
    skipped = 0
    for path in entries:
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        if not _BAND_NAME.match(path.name):
            skipped += 1
            continue
        candidate = path.resolve()
        if candidate.parent != root:
            skipped += 1
            continue
        try:
            stat = candidate.stat()
        except OSError:
            continue
        band_files[path.name] = (candidate, stat.st_size, stat.st_mtime)
    return band_files, skipped


def _band_name(image_path: Any) -> str | None:
    """引用行的 `image_path` → 白名单文件名；不像底图的返回 None（不参与删文件）。"""
    text = str(image_path or "").strip().replace("\\", "/")
    if not text:
        return None
    name = text.rsplit("/", 1)[-1]
    return name if _BAND_NAME.match(name) else None


def band_name_of(image_path: Any) -> str | None:
    """公开版 `_band_name`：手动清理端点用它判断引用行是不是底图。"""
    return _band_name(image_path)


def band_file_sizes(band_dir: str | Path) -> dict[str, int]:
    """底图目录里过白名单 + 沙箱校验的文件 → 字节数（手动清理端点只认这批名字）。"""
    files, _skipped = _scan(Path(band_dir).resolve())
    return {name: size for name, (_path, size, _mtime) in files.items()}


def band_delete_files(band_dir: str | Path, names: Sequence[str]) -> tuple[int, int]:
    """删一批白名单文件，返回 (张数, 字节)；目录 / 其它文件一概不碰。"""
    root = Path(band_dir).resolve()
    sizes = band_file_sizes(root)
    deleted = 0
    freed = 0
    for name in names:
        size = sizes.get(str(name))
        if size is None:
            continue
        if _unlink(root / str(name)):
            deleted += 1
            freed += size
    return deleted, freed


def band_storage(
    band_dir: str | Path,
    references: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """按会话汇总底图占用（票 #227：池页顶栏 / 会话组头 / #235 的 80% 提示共用）。

    返回 `{"images": 目录里白名单文件总数, "bytes": 这些文件的总字节,
    "sessions": {session_id: {"images", "bytes", "picked_images", "unpicked_images"}}}`。

    口径：文件侧只数**白名单 + 沙箱内 + 真实存在**的文件（缺图引用不虚报占用）；
    `images` / `bytes` 是**磁盘占用**（含暂未被引用的孤儿文件，自动清理会收），
    而 `sessions` 只统计被引用到的文件，供组头展示。同一张图被多行引用时只算一张；
    只要还有一行未挑，这张图就归 `unpicked_images`（容量兜底不许动它）。
    """
    root = Path(band_dir).resolve()
    files, _skipped = _scan(root)
    per_session: dict[int, dict[str, set[str]]] = {}
    for row in references:
        name = _band_name(row.get("image_path"))
        if name is None or name not in files:
            continue
        raw_session = row.get("session_id")
        if raw_session is None:
            continue
        bucket = per_session.setdefault(
            int(raw_session), {"picked": set(), "unpicked": set()}
        )
        if row.get("picked_at"):
            bucket["picked"].add(name)
        else:
            bucket["unpicked"].add(name)
    sessions: dict[int, dict[str, int]] = {}
    for session_id, bucket in per_session.items():
        names = bucket["picked"] | bucket["unpicked"]
        picked = len(bucket["picked"] - bucket["unpicked"])
        sessions[session_id] = {
            "images": len(names),
            "bytes": sum(files[name][1] for name in names),
            "picked_images": picked,
            "unpicked_images": len(names) - picked,
        }
    return {
        "images": len(files),
        "bytes": sum(size for _path, size, _mtime in files.values()),
        "sessions": sessions,
    }


def _unlink(path: Path) -> bool:
    """删一个文件；被占用 / 已消失都当跳过，绝不中断整轮清理。"""
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _all_picked(rows: Sequence[Mapping[str, Any]] | None) -> bool:
    """一张图被多行引用时，**所有**引用行都已挑才允许自动删（未挑图是改句依据）。"""
    return bool(rows) and all(bool(row.get("picked_at")) for row in rows)


def cleanup_bands(
    band_dir: str | Path,
    *,
    list_references: Callable[[], Sequence[Mapping[str, Any]]],
    clear_paths: Callable[[Sequence[int]], int],
    ttl_days: int = DEFAULT_TTL_DAYS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    orphan_min_age_s: float = DEFAULT_ORPHAN_MIN_AGE_S,
    dry_run: bool = False,
    now: float | None = None,
) -> BandCleanupResult:
    """跑一轮底图清理（孤儿 + 缺图对账 + 已挑 TTL + 容量兜底），写一行汇总日志。

    - `list_references()`：引用行（`id / session_id / image_path / picked_at / updated_at`），
      由 Store 提供；
    - `clear_paths(ids)`：按 id 批量把 `image_path` 置 NULL，返回实际行数；
    - `ttl_days`：已挑句底图保留天数，**从 `picked_at` 起算**；`0` = 关闭；
    - `max_bytes`：底图目录总量上限，超限只按 mtime 从旧到新淘汰**已挑句**的图，
      删到 `TARGET_RATIO`（80%）；`0` = 关闭；
    - `orphan_min_age_s <= 0`：关闭孤儿清扫（**不是**「删光」—— 0 岁门会误删在途写入）；
    - `dry_run=True`：不删文件、不置空 DB，只在结果里报预计数。

    铁律（spec §Solution）：**未挑句的底图永不自动删**。
    """
    moment = time.time() if now is None else float(now)
    root = Path(band_dir).resolve()  # D-L1：调用方按 store.db_path.parent / "subtitle_bands"
    files, skipped = _scan(root)
    if skipped:
        log.warning("底图目录有 %d 个非白名单文件，已跳过（不删）", skipped)

    # 引用行按「文件名」归组：一个文件可能被多行引用（改句 / 接管后仍指着同一张图）。
    by_name: dict[str, list[Mapping[str, Any]]] = {}
    for row in list_references():
        name = _band_name(row.get("image_path"))
        if name is None:
            continue  # 非白名单引用不是底图，不删文件也不置空
        by_name.setdefault(name, []).append(row)

    clear_ids: list[int] = []
    doomed: dict[str, tuple[Path, int]] = {}  # 文件名 → (路径, 字节)

    # 规则 2：孤儿清扫 —— 目录里有、引用表没有、且够老（24h 门避开在途写入）。
    if orphan_min_age_s > 0:
        cutoff = moment - float(orphan_min_age_s)
        for name, (path, size, mtime) in files.items():
            if name in by_name:
                continue
            if mtime <= cutoff:
                doomed[name] = (path, size)

    # 规则 3：缺图对账 —— 引用行指向的文件不存在 → 置空路径（只记数，不报错）。
    for name, rows in by_name.items():
        if name in files:
            continue
        clear_ids.extend(int(row["id"]) for row in rows)

    # 规则 4：已挑句 TTL —— 从 `picked_at` 起算（改句 / 重识别只刷 updated_at，不续命）。
    if ttl_days > 0:
        cutoff_dt = datetime.fromtimestamp(moment) - timedelta(days=int(ttl_days))
        for name, rows in by_name.items():
            if name not in files or name in doomed or not _all_picked(rows):
                continue
            stamps = [parse_ts(row.get("picked_at")) for row in rows]
            if any(stamp is None for stamp in stamps):
                continue  # 时间戳坏掉：不猜、不删
            if all(stamp < cutoff_dt for stamp in stamps):
                path, size, _mtime = files[name]
                doomed[name] = (path, size)
                clear_ids.extend(int(row["id"]) for row in rows)

    # 规则 5：容量兜底 —— 总量超上限 → 按 mtime 从旧到新只淘汰已挑句，删到 80%。
    # 未挑句的图一张都不许动；如果降不下来，只记 warning 提示用户手动清。
    if max_bytes > 0:
        total = sum(
            size for name, (_path, size, _mtime) in files.items() if name not in doomed
        )
        target = int(max_bytes * TARGET_RATIO)
        if total > max_bytes:
            eligible = sorted(
                (files[name][2], name, files[name][1])
                for name in files
                if name not in doomed and _all_picked(by_name.get(name))
            )
            for _mtime, name, size in eligible:
                if total <= target:
                    break
                path, _s, _m = files[name]
                doomed[name] = (path, size)
                total -= size
                clear_ids.extend(int(row["id"]) for row in by_name[name])
        if total > max_bytes:
            log.warning(
                "底图仍超上限（%.1f MB > %.1f MB）：%d 张未挑句底图不自动删，请手动清理",
                total / 1024 / 1024,
                max_bytes / 1024 / 1024,
                len([name for name in files if name not in doomed]),
            )

    planned_files = len(doomed)
    planned_bytes = sum(size for _path, size in doomed.values())
    planned_cleared = len(clear_ids)

    deleted = 0
    freed = 0
    cleared = 0
    if not dry_run:
        # 规则 6：先置空 DB（提交），再 unlink。
        if clear_ids:
            cleared = int(clear_paths(sorted(set(clear_ids))))
        for path, size in doomed.values():
            if _unlink(path):
                deleted += 1
                freed += size

    kept = len(_scan(root)[0])
    if dry_run:
        log.info(
            "底图清理（dry-run）：预计可删 %d 张/%.1f MB、置空 %d 行；保留 %d 张",
            planned_files,
            planned_bytes / 1024 / 1024,
            planned_cleared,
            kept,
        )
    else:
        log.info(
            "底图清理：删 %d 张/%.1f MB、置空 %d 行、保留 %d 张",
            deleted,
            freed / 1024 / 1024,
            cleared,
            kept,
        )
    return BandCleanupResult(
        deleted_files=deleted,
        freed_bytes=freed,
        cleared_rows=cleared,
        kept_files=kept,
        dry_run=bool(dry_run),
        planned_files=planned_files,
        planned_bytes=planned_bytes,
        planned_cleared=planned_cleared,
    )
