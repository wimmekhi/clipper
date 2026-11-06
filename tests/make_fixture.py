"""用 13 条精准样本拼一个假「整集」，配一份带漂移偏移的双语 SRT 和一份双语 ASS，测试全流程。"""
import csv, json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SAMP = ROOT / "core-assets/local-33-core-assets-v1/00-handover/07-precision-samples"
QC = ROOT / "core-assets/local-33-core-assets-v1/01-core-project/phase2/qc.csv"
out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/testlib") / "Sample Show" / "S01"
out.mkdir(parents=True, exist_ok=True)

ZH = {"W1": "你在干嘛，这么要紧？", "W2": "你在外面干什么？", "W3": "你在干什么？", "W4": "你在外面这儿干嘛？",
      "W5": "你这周末干嘛？", "W6": "你在里面干嘛？", "W7": "你在这儿干什么？", "I1": "这是你丈夫吗？",
      "I2": "这是你家吗？", "I3": "这是你的小把戏吗？", "H1": "这是谁的车？", "H2": "那是谁的错？", "H3": "这是谁的？"}
rows = list(csv.DictReader(open(QC, encoding="utf-8-sig")))
GAP = 1.5
inputs, filt, t, truth, cues = [], [], 3.0, [], []
inputs += ["-f", "lavfi", "-t", "3.0", "-i", "color=black:s=720x406:r=24000/1001", "-f", "lavfi", "-t", "3.0", "-i", "anullsrc=r=48000:cl=stereo"]
segs = ["[g0][1:a]"]
filt.append("[0:v]setsar=1[g0]")
n = 2
for i, r in enumerate(rows):
    f = SAMP / r["成品文件"]
    d = float(json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_format", "-print_format", "json", str(f)],
                                        capture_output=True, text=True).stdout)["format"]["duration"])
    inputs += ["-i", str(f)]
    filt.append(f"[{n}:v]scale=720:406,setsar=1,fps=24000/1001[v{n}]")
    filt.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=stereo[a{n}]")
    segs.append(f"[v{n}][a{n}]")
    n += 1
    inputs += ["-f", "lavfi", "-t", str(GAP), "-i", "color=black:s=720x406:r=24000/1001",
               "-f", "lavfi", "-t", str(GAP), "-i", "anullsrc=r=48000:cl=stereo"]
    filt.append(f"[{n}:v]setsar=1[g{n}]")
    segs.append(f"[g{n}][{n+1}:a]")
    n += 2
    true_start = t + 0.3
    true_end = t + d - 0.35
    off = -2.0 + 0.45 * i           # 漂移：-2.0s → +3.4s
    cues.append((true_start + off, true_end + off + 0.4, r["目标句"].replace("\n", " "), ZH[r["编号"]]))
    truth.append(dict(id=r["编号"], true_start=round(true_start, 3), true_end=round(true_end, 3)))
    t += d + GAP
fc = ";".join(filt) + ";" + "".join(segs) + f"concat=n={len(segs)}:v=1:a=1[v][a]"
cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
       "-c:v", "libx264", "-crf", "23", "-g", "24", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
       str(out / "S01E01.mp4")]
subprocess.run(cmd, check=True)

def ts(x, sep=","):
    ms = int(round(x * 1000)); return f"{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d}{sep}{ms%1000:03d}"
with open(out / "S01E01.srt", "w", encoding="utf-8") as fh:
    for k, (a, b, en, zh) in enumerate(cues, 1):
        fh.write(f"{k}\n{ts(a)} --> {ts(b)}\n{zh}\n{en}\n\n")
# 第二集：同一视频 + 双语 ASS（中英分两条 Dialogue，再加一条特效行）
import shutil; shutil.copy(out / "S01E01.mp4", out / "S01E02.mp4")
def ats(x):
    cs = int(round(x * 100)); return f"{cs//360000}:{cs//6000%60:02d}:{cs//100%60:02d}.{cs%100:02d}"
with open(out / "S01E02.ass", "w", encoding="utf-8") as fh:
    fh.write("[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name, Fontname\nStyle: Default,Arial\n\n[Events]\n"
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    fh.write(f"Dialogue: 0,0:00:00.50,0:00:03.00,Sign,,0,0,0,,{{\\pos(100,100)}}片头特效字\n")
    for a, b, en, zh in cues:
        fh.write(f"Dialogue: 0,{ats(a)},{ats(b)},Default,,0,0,0,,{{\\fs20}}{zh}\\N{{\\i1}}{en}{{\\i0}}\n")
json.dump(truth, open(out / "truth.json", "w"), indent=1)
print("ok", t)
