"""命令行：适合在 Mac 上整夜跑对齐。

  python -m clipper.cli scan  <媒体库根目录>
  python -m clipper.cli align <媒体库根目录> [--model small] [--engine faster|mlx] [--no-mirror]
  python -m clipper.cli search <媒体库根目录> "what are you doing" [--exact] [--ordered]
"""
import argparse
import sys
import time

from .library import Library
from . import align
from .search import Searcher


def main(argv=None):
    ap = argparse.ArgumentParser(prog="clipper")
    ap.add_argument("cmd", choices=["scan", "align", "search"])
    ap.add_argument("root")
    ap.add_argument("query", nargs="?")
    ap.add_argument("--model", default=None)
    ap.add_argument("--engine", default=None)
    ap.add_argument("--no-mirror", action="store_true")
    ap.add_argument("--exact", action="store_true")
    ap.add_argument("--ordered", action="store_true")
    a = ap.parse_args(argv)
    lib = Library(a.root)
    print("扫描…", lib.scan())
    if a.cmd == "align":
        model = a.model or lib.get_setting("align_model", "small")
        rows = lib.db.execute("SELECT * FROM media WHERE align_status!='done' AND rel_video IS NOT NULL "
                              "ORDER BY show, season, episode").fetchall()
        print(f"待对齐 {len(rows)} 集，模型 {model}，引擎 {a.engine or align.default_engine()}")
        for i, m in enumerate(rows, 1):
            t = time.time()
            try:
                d = align.align_media(lib, m, model=model, engine=a.engine, mirror=not a.no_mirror)
                print(f"[{i}/{len(rows)}] {m['key']}  {d['summary']}")
            except Exception as ex:
                lib.db.execute("UPDATE media SET align_status='error', align_msg=? WHERE id=?", (str(ex), m["id"]))
                lib.db.commit()
                print(f"[{i}/{len(rows)}] {m['key']}  失败：{ex}", file=sys.stderr)
    elif a.cmd == "search":
        for h in Searcher(lib).search(a.query or "", exact=a.exact, ordered=a.ordered)[:30]:
            print(f"{h.tier}  {h.score:5.2f}  {h.label:28s} {h.en}  |  {h.zh}")


if __name__ == "__main__":
    main()
