"""一次性修复已落库 OCR 待选句的空格粘连（#221）。

默认 dry-run；`--apply` 才写库（写前用 SQLite backup 留一份 `<db>.bak-<时间>`）。
只改 `source='ocr'` 且**未挑**的句子（已挑句改文本会作废挑词，跳过并列出）。
不改 CC/ASR 文本，不改采样参数。

用法（工具目录下）：
    python repair_ocr_spacing.py --db data/capture.db          # 只看会改什么
    python repair_ocr_spacing.py --db data/capture.db --apply  # 执行
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

import corrections
import kernel
from store import Store


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


def main() -> int:
    parser = argparse.ArgumentParser(description="修复已落库 OCR 待选句的空格粘连")
    parser.add_argument("--db", default=str(Path(__file__).resolve().parent / "data" / "capture.db"))
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

    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "select id, session_id, text, picked_at from subtitle_backlog "
            "where source='ocr' order by id"
        ).fetchall()
    finally:
        con.close()

    changes: list[tuple[int, int, str, str]] = []
    picked: list[tuple[int, str]] = []
    for row_id, session_id, text, picked_at in rows:
        new = corrections.restore_spaces(text or "", known, common)
        if new == (text or ""):
            continue
        if picked_at:
            picked.append((row_id, text or ""))
            continue
        changes.append((int(row_id), int(session_id), text or "", new))

    print(f"扫描 OCR 句 {len(rows)} 条：可修 {len(changes)} 条；已挑跳过 {len(picked)} 条")
    for row_id, session_id, old, new in changes:
        print(f"  #{row_id} (会话 {session_id}): {old!r} -> {new!r}")
    for row_id, old in picked:
        print(f"  跳过已挑 #{row_id}: {old!r}（改文本会作废挑词）")

    if not args.apply:
        print("dry-run：没有写库；确认后加 --apply")
        return 0

    backup = _backup(db_path)
    print(f"已备份：{backup}")
    store = Store(db_path)
    updated = 0
    try:
        for row_id, session_id, _old, new in changes:
            try:
                store.update_subtitle_text(session_id, row_id, new)
                updated += 1
            except ValueError as exc:
                print(f"  #{row_id} 跳过：{exc}")
    finally:
        store.close()
    print(f"已更新 {updated} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
