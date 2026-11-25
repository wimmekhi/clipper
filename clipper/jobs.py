"""导出任务：把采集包或单条导出成视频。

1.3.4：只输出视频——目录 = 导出根/文件夹层级/包名/*.mp4，不再有「说明」文件夹、manifest.json 和预设名那一层。
（只有一次勾了多个版本时才按版本分子文件夹。）
"""
from __future__ import annotations

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
                 progress=None, make_compilation=True, cancel=lambda: False, base_n=0, total=None) -> dict:
    """items: [{row, pattern, tier, matched}]。只写视频文件。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    flat = len(presets) == 1
    sub = (lambda p: out_dir) if flat else (lambda p: out_dir / p)
    done = {p: [] for p in presets}
    total = total if total is not None else len(items) * len(presets)
    n = base_n
    errors = []
    count = 0
    for i, it in enumerate(items, 1):
        row = it["row"]
        meta = item_meta(lib, row, it.get("pattern", ""), it.get("tier", ""), it.get("matched", ""))
        video = lib.abs(row["rel_video"])
        # 文件名 = 英文台词（单条可在导出框里改），不加序号；重名就像 Windows 那样在后面加 (2)、(3)…
        nm = export.clip_name(it.get("name") or row["en"])
        exts = [export.out_ext(p, video) if video else ".mp4" for p in presets]

        def taken(b):
            return any((sub(p) / f"{b}{x}").exists() for p, x in zip(presets, exts))
        base = nm
        for k in range(2, 10000):
            if not taken(base):
                break
            base = f"{nm} ({k})"
        if not video or not video.exists():
            errors.append(f"{meta['source']} #{row['idx']}: 没有视频文件")
            n += len(presets)
            continue
        ok_any = False
        for p in presets:
            if cancel():
                return dict(ok=False, cancelled=True, errors=errors, count=count, out=str(out_dir))
            f = sub(p) / f"{base}{export.out_ext(p, video)}"
            try:
                export.export_clip(video, meta["start"], meta["end"], f, p, style, en=row["en"],
                                   zh=row["zh"] or "", matched=meta["matched"], title=meta["pattern"] or "",
                                   source=meta["source"])
                done[p].append(f)
                ok_any = True
            except Exception as ex:   # noqa
                errors.append(f"{meta['source']} #{row['idx']} [{p}]: {ex}")
            n += 1
            if progress:
                progress(n, total, f"{meta['source']} · {row['en'][:40]}")
        count += 1 if ok_any else 0
    comp = {}
    if make_compilation:
        for p, files in done.items():
            if len(files) >= 2 and not (PRESETS[p].get("lossless") or PRESETS[p].get("precise")):   # 无损版各集编码不同，不能直接拼
                try:
                    comp[p] = str(export.concat(files, out_dir / f"合集_{p}.mp4").name)
                except Exception as ex:   # noqa
                    errors.append(f"合集 [{p}]: {ex}")
    return dict(ok=not errors, count=count, errors=errors, compilations=comp, out=str(out_dir), n=n)


def pack_dir(lib, pack_id, export_root: Path, rel_from=None) -> Path:
    """采集包的导出目录 = 导出根/文件夹层级/包名（每一级都换掉文件名里不能用的字符）。"""
    path = lib.pack_path(pack_id)
    if rel_from:
        path = path[len(rel_from):]
    return Path(export_root).joinpath(*[export.dir_name(x) for x in path]) if path else Path(export_root)


def export_packs(lib, jobs_: list[tuple[int, Path]], presets: list[str], style: Style,
                 progress=None, cancel=lambda: False) -> dict:
    """导出多个包（导出整个文件夹 = 下面所有包，保持结构）：jobs_ = [(包 id, 输出目录)]。"""
    plan = [(pid, Path(d), pack_to_items(lib, pid)) for pid, d in jobs_]
    total = sum(len(it) for _, _, it in plan) * len(presets)
    errors, count, n = [], 0, 0
    for pid, d, items in plan:
        if not items:
            continue
        r = export_items(lib, items, d, presets, style, progress, False, cancel, base_n=n, total=total)
        n = r.get("n", n)
        errors += r.get("errors", [])
        count += r.get("count", 0)
        if r.get("cancelled"):
            return dict(ok=False, cancelled=True, errors=errors, count=count)
    return dict(ok=not errors, count=count, errors=errors, compilations={}, packs=len(plan))


def pack_to_items(lib, pack_id):
    from .search import match_span
    items = []
    for r in lib.pack_items(pack_id):
        matched = match_span(r["pattern"], r["en"]) if r["pattern"] else ""
        items.append(dict(row=r, pattern=r["pattern"], tier=r["tier"], matched=matched))
    return items
