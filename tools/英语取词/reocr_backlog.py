"""按底图重跑存量 OCR 待选句（#222）。

背景：RapidOCR 出厂 `width_height_ratio=8` 会跳过检测头，字幕带被压成一行识别
（丢空格/丢字母）。参数已在 `ocr_sampler.OCR_ENGINE_PARAMS` 修正；本脚本用同一
引擎把**已落库**的底图重跑一遍，清掉存量错误。

纪律（与 repair_ocr_spacing.py 同款）：
- 默认 dry-run；`--apply` 才写库，写前 SQLite backup；
- 只改 `source='ocr'` 且**未挑**的行（已挑句改文本会作废挑词，跳过并列出）；
- 只在 OOV 词元数**严格下降**且文本变化时回写（不拿不确定的结果冒险）；
- 同一张底图检出多行时，选「OOV 少 + 与原句最像」的那一行，不硬拼两行字幕。

用法（工具目录下）：
    python reocr_backlog.py --db data/capture.db              # 只看会改什么
    python reocr_backlog.py --db data/capture.db --apply      # 执行
"""
from __future__ import annotations

import argparse
import difflib
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

import corrections
import kernel
from ocr_sampler import BandOcrEngine
from store import Store, is_latin_line, one_line


def _backup(db_path: Path) -> Path:
    backup = db_path.with_name(db_path.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(backup)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup


def _load_band(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"))


def _reocr_text(
    engine, band: np.ndarray, old: str, known: frozenset[str], common: frozenset[str]
) -> tuple[str, float | None]:
    """底图 → 文本。多行时按（OOV 少、与原句像、词元多、置信度高）挑一行。"""
    candidates: list[tuple[str, float]] = []
    for text, conf in engine(band):
        text = one_line(corrections.clean_ocr_punctuation(str(text)))
        if not text or not is_latin_line(text):
            continue
        text = one_line(corrections.restore_spaces(text, known, common))
        candidates.append((text, float(conf)))
    if not candidates:
        return "", None

    def rank(item: tuple[str, float]) -> tuple[int, float, int, float]:
        text, conf = item
        oov = len(corrections.oov_tokens(text, known))
        sim = difflib.SequenceMatcher(None, old.lower(), text.lower()).ratio()
        return (oov, -sim, -len(text.split()), -conf)

    best_text, _best_conf = min(candidates, key=rank)
    return best_text, statistics.fmean(conf for _text, conf in candidates)


def main() -> int:
    parser = argparse.ArgumentParser(description="按底图重跑存量 OCR 待选句（#222）")
    parser.add_argument("--db", default=str(Path(__file__).resolve().parent / "data" / "capture.db"))
    parser.add_argument("--session", type=int, default=None, help="只处理某个会话")
    parser.add_argument("--apply", action="store_true", help="真正写库（缺省只 dry-run）")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"数据库不存在：{db_path}")
        return 2

    known, common = kernel.correction_wordlists()
    if not known:
        print("词表不可用（data/common_words.txt / words_10k.txt），未做任何修改")
        return 2

    sql = (
        "select id, session_id, text, image_path, picked_at from subtitle_backlog "
        "where source='ocr' and image_path is not null and image_path != ''"
    )
    params: list[object] = []
    if args.session is not None:
        sql += " and session_id = ?"
        params.append(args.session)
    sql += " order by id"
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    engine = BandOcrEngine()
    band_dir = (db_path.parent / "subtitle_bands").resolve()
    changes: list[tuple[int, int, str, str, float | None, int, int]] = []
    picked: list[tuple[int, str]] = []
    skipped: list[tuple[int, str]] = []

    for row_id, session_id, old, image_path, picked_at in rows:
        old = old or ""
        name = str(image_path).replace("\\", "/").split("/")[-1]
        image_file = (band_dir / name).resolve()
        if image_file.parent != band_dir or not image_file.is_file():
            skipped.append((int(row_id), "底图缺失"))
            continue
        try:
            new, score = _reocr_text(engine, _load_band(image_file), old, known, common)
        except Exception as exc:  # noqa: BLE001 - 单行失败继续处理其余行
            skipped.append((int(row_id), f"识别失败 {type(exc).__name__}"))
            continue
        if not new:
            skipped.append((int(row_id), "重识别无拉丁文本"))
            continue
        if new == old:
            continue
        old_oov = len(corrections.oov_tokens(old, known))
        new_oov = len(corrections.oov_tokens(new, known))
        if new_oov >= old_oov:
            skipped.append((int(row_id), f"OOV 未下降 {old_oov}→{new_oov}"))
            continue
        if picked_at:
            picked.append((int(row_id), old))
            continue
        changes.append((int(row_id), int(session_id), old, new, score, old_oov, new_oov))

    print(
        f"扫描 OCR 句 {len(rows)} 条：可改 {len(changes)} 条；"
        f"已挑跳过 {len(picked)} 条；其余跳过 {len(skipped)} 条"
    )
    for row_id, session_id, old, new, score, old_oov, new_oov in changes:
        conf = f"{score:.3f}" if score is not None else "-"
        print(f"  #{row_id} (会话 {session_id}, conf {conf}): {old!r} -> {new!r}  OOV {old_oov}→{new_oov}")
    for row_id, old in picked:
        print(f"  跳过已挑 #{row_id}: {old!r}（改文本会作废挑词）")
    if skipped:
        for row_id, reason in skipped:
            print(f"  跳过 #{row_id}: {reason}")

    if not args.apply:
        print("dry-run：没有写库；确认后加 --apply")
        return 0

    backup = _backup(db_path)
    print(f"已备份：{backup}")
    store = Store(db_path)
    updated = 0
    try:
        for row_id, session_id, _old, new, score, _old_oov, _new_oov in changes:
            try:
                store.update_subtitle_ocr(
                    session_id,
                    row_id,
                    text=new,
                    score=score,
                    suspicious=corrections.looks_suspicious(new, known),
                )
                updated += 1
            except ValueError as exc:
                print(f"  #{row_id} 跳过：{exc}")
            except sqlite3.IntegrityError:
                # 两句修成同一句：唯一键 (session_id, sentence_key) 冲突。
                # 另一行已经有这句了，保留本行原文（宁可少改，不删数据）。
                print(f"  #{row_id} 跳过：修后与同会话既有句重复")
    finally:
        store.close()
    print(f"已更新 {updated} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
