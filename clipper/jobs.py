"""导出任务：把采集包或单条导出成三种预设 + 说明文件 + 合集。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import export
from .export import Style, PRESETS


def item_meta(lib, row, pattern="", tier="", matched=""):
    key = row["media_key"] if "media_key" in row.keys() else row["key"]
    s, e, src = lib.final_span(row, key)
    if row["kind"] == "tv":
        source = f"{row['show']} S{row['season']:02d}E{row['episode']:02d}"
    else:
        source = row["show"]
    # 词级时间（相对片段开头），网站可以逐词高亮
    words = [dict(w=w["w"], start=round(w["start"] - s, 3), end=round(w["end"] - s, 3))
             for w in lib.words(key) if w["start"] >= s - 0.01 and w["end"] <= e + 0.01]
    return dict(media_key=key, idx=row["idx"], source=source, show=row["show"],
                season=row["season"], episode=row["episode"], en=row["en"], zh=row["zh"] or "",
                pattern=pattern, tier=tier, matched=matched, start=round(s, 3), end=round(e, 3),
                duration=round(e - s, 3), timing=src, words=words)


def export_items(lib, items: list[dict], out_dir: Path, presets: list[str], style: Style,
                 progress=None, make_compilation=True, cancel=lambda: False) -> dict:
    """items: [{row, pattern, tier, matched}]"""
    out_dir.mkdir(parents=True, exist_ok=True)
    done = {p: [] for p in presets}
    manifest = []
    total = len(items) * len(presets)
    n = 0
    errors = []
    for i, it in enumerate(items, 1):
        row = it["row"]
        meta = item_meta(lib, row, it.get("pattern", ""), it.get("tier", ""), it.get("matched", ""))
        video = lib.abs(row["rel_video"])
        base = f"{i:03d}_{export.safe_name(meta['source'])}_{row['idx']:04d}"
        meta["files"] = {}
        if not video or not video.exists():
            errors.append(f"{meta['source']} #{row['idx']}: 没有视频文件")
            n += len(presets)
            continue
        for p in presets:
            if cancel():
                return dict(ok=False, cancelled=True, errors=errors)
            f = out_dir / p / f"{base}.mp4"
            try:
                export.export_clip(video, meta["start"], meta["end"], f, p, style, en=row["en"],
                                   zh=row["zh"] or "", matched=meta["matched"], title=meta["pattern"] or "",
                                   source=meta["source"])
                done[p].append(f)
                meta["files"][p] = f"{p}/{f.name}"
            except Exception as ex:   # noqa
                errors.append(f"{meta['source']} #{row['idx']} [{p}]: {ex}")
            n += 1
            if progress:
                progress(n, total, f"{meta['source']} · {row['en'][:40]}")
        (out_dir / "说明").mkdir(exist_ok=True)
        (out_dir / "说明" / f"{base}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
        manifest.append(meta)
    comp = {}
    if make_compilation:
        for p, files in done.items():
            if len(files) >= 2:
                try:
                    comp[p] = str(export.concat(files, out_dir / f"合集_{p}.mp4").name)
                except Exception as ex:   # noqa
                    errors.append(f"合集 [{p}]: {ex}")
    (out_dir / "manifest.json").write_text(json.dumps(dict(created=time.time(), presets=presets,
                                                           compilations=comp, items=manifest),
                                                      ensure_ascii=False, indent=1), "utf-8")
    return dict(ok=not errors, count=len(manifest), errors=errors, compilations=comp, out=str(out_dir))


def pack_to_items(lib, pack_id):
    from .search import parse_query, _regex, norm_tokens
    items = []
    for r in lib.pack_items(pack_id):
        matched = ""
        if r["pattern"]:
            q = parse_query(r["pattern"])
            toks = norm_tokens(r["en"])
            m = _regex(q.parts).search(" " + " ".join(toks) + " ")
            if m:
                a = len((" " + " ".join(toks) + " ")[: m.start()].split())
                matched = " ".join(toks[a:a + len(m.group(0).split())])
        items.append(dict(row=r, pattern=r["pattern"], tier=r["tier"], matched=matched))
    return items
