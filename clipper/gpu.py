"""Windows N 卡加速（1.1.0）：faster-whisper 走 CUDA。

- ctranslate2 的 Windows 包自带 CUDA 内核和 cudnn64_9.dll 引导库，但 cuBLAS、cuDNN 的子库不随包（太大）。
- 第一次用时从 PyPI 国内镜像拉 NVIDIA 官方的 nvidia-cublas-cu12 / nvidia-cudnn-cu12 轮子，
  只按字节范围下载需要的那几个 DLL（不下 cudnn_adv 这类用不到的），断点续传，换镜像接着下，CRC 校验。
- 放在本机 %LOCALAPPDATA%\\clipper\\cuda12（不放移动硬盘：只有这台 Windows 用）。
- 识别子进程里先 prepare() 预加载这些 DLL，再用 device="cuda"。
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import zlib
from pathlib import Path

# cuDNN 版本必须和 ctranslate2 自带的 cudnn64_9.dll 一致（ctranslate2 4.9.1 → 9.10.2.21），requirements 里钉死了 ctranslate2
WHEELS = [
    dict(name="cuBLAS 12.4",
         path="packages/e2/2a/4f27ca96232e8b5269074a72e03b4e0d43aa68c9b965058b1684d07c6ff8/"
              "nvidia_cublas_cu12-12.4.5.8-py3-none-win_amd64.whl",
         size=396895858,
         members=["nvidia/cublas/bin/cublasLt64_12.dll", "nvidia/cublas/bin/cublas64_12.dll"]),
    dict(name="cuDNN 9.10",
         path="packages/3d/90/0bd6e586701b3a890fd38aa71c387dab4883d619d6e5ad912ccbd05bfd67/"
              "nvidia_cudnn_cu12-9.10.2.21-py3-none-win_amd64.whl",
         size=692992268,
         members=["nvidia/cudnn/bin/cudnn_graph64_9.dll", "nvidia/cudnn/bin/cudnn_ops64_9.dll",
                  "nvidia/cudnn/bin/cudnn_cnn64_9.dll", "nvidia/cudnn/bin/cudnn_heuristic64_9.dll",
                  "nvidia/cudnn/bin/cudnn_engines_runtime_compiled64_9.dll",
                  "nvidia/cudnn/bin/cudnn_engines_precompiled64_9.dll"]),
]
# 预加载顺序：被依赖的在前
LOAD_ORDER = ["cudnn_graph64_9.dll", "cudnn_ops64_9.dll", "cudnn_cnn64_9.dll", "cudnn_heuristic64_9.dll",
              "cudnn_engines_runtime_compiled64_9.dll", "cudnn_engines_precompiled64_9.dll",
              "cublasLt64_12.dll", "cublas64_12.dll"]
MIRRORS = ["https://pypi.tuna.tsinghua.edu.cn/", "https://mirrors.aliyun.com/pypi/",
           "https://mirrors.cloud.tencent.com/pypi/", "https://mirrors.ustc.edu.cn/pypi/",
           "https://repo.huaweicloud.com/repository/pypi/", "https://files.pythonhosted.org/"]
APPROX_MB = 885          # 要下载的压缩字节，约数（界面提示用）
MIN_DRIVER = 528         # CUDA 12 在 Windows 上要求的驱动大版本
MANIFEST = "manifest.json"
UA = "clipper-cuda-setup"


def is_windows() -> bool:
    return sys.platform == "win32"


def cuda_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") if is_windows() else None
    return Path(base or (Path.home() / ".cache")) / "clipper" / "cuda12"


# ---------------- 显卡 ----------------

_GPU_CACHE: list = []


def nvidia_gpu() -> dict | None:
    """有 N 卡就返回 {name, mem_mb, driver, driver_major}，否则 None。只在 Windows 上查。"""
    if _GPU_CACHE:
        return _GPU_CACHE[0]
    info = None
    if is_windows():
        cands = ["nvidia-smi"]
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        cands.append(os.path.join(sysroot, "System32", "nvidia-smi.exe"))
        for exe in cands:
            try:
                r = subprocess.run([exe, "--query-gpu=name,memory.total,driver_version",
                                    "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                   timeout=8, creationflags=0x08000000)
            except Exception:
                continue
            line = (r.stdout or "").strip().splitlines()
            if r.returncode == 0 and line:
                p = [x.strip() for x in line[0].split(",")]
                try:
                    major = int(p[2].split(".")[0])
                except Exception:
                    major = 0
                info = dict(name=p[0], mem_mb=int(float(p[1])) if len(p) > 1 and p[1] else 0,
                            driver=p[2] if len(p) > 2 else "", driver_major=major)
                break
    _GPU_CACHE.append(info)
    return info


def runtime_ready() -> bool:
    d = cuda_dir()
    try:
        man = json.loads((d / MANIFEST).read_text("utf-8"))
    except Exception:
        return False
    for n in LOAD_ORDER:
        f = d / n
        if not f.is_file() or f.stat().st_size != man.get(n):
            return False
    return True


def status() -> dict:
    """界面用：{state, text}。state: na / no_gpu / old_driver / need_runtime / ready / broken"""
    if not is_windows():
        return dict(state="na", text="只有 Windows + NVIDIA 显卡需要（Mac 用 Apple 芯片 GPU）")
    g = nvidia_gpu()
    if not g:
        return dict(state="no_gpu", text="没检测到 NVIDIA 显卡（或没装驱动），用 CPU")
    if g["driver_major"] and g["driver_major"] < MIN_DRIVER:
        return dict(state="old_driver",
                    text=f"{g['name']}：驱动 {g['driver']} 太旧，升级到 {MIN_DRIVER} 以上才能用显卡")
    if _BROKEN:
        return dict(state="broken", text=f"{g['name']}：显卡出错已退回 CPU（{_BROKEN[:80]}）")
    if not runtime_ready():
        return dict(state="need_runtime",
                    text=f"{g['name']}（{g['mem_mb']//1024 or '?'} GB）：还没下载 CUDA 运行库（约 {APPROX_MB} MB）")
    return dict(state="ready", text=f"{g['name']}（{g['mem_mb']//1024 or '?'} GB）：CUDA 运行库已就绪")


# ---------------- 加载 ----------------

_BROKEN: str | None = None
_PREPARED = False


def mark_broken(why: str):
    global _BROKEN
    _BROKEN = (why or "未知错误")[:300]
    print(f"[clipper] CUDA 不可用，改用 CPU：{_BROKEN}", flush=True)


def prepare() -> None:
    """把运行库目录加进 DLL 搜索路径并按顺序预加载（之后 ctranslate2 按名字找就能找到）。"""
    global _PREPARED
    if _PREPARED:
        return
    d = str(cuda_dir())
    try:
        os.add_dll_directory(d)
    except Exception:
        pass
    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    import ctypes
    for n in LOAD_ORDER:
        ctypes.WinDLL(os.path.join(d, n))
    _PREPARED = True


def usable() -> bool:
    """现在能不能走 CUDA（不加载，只看条件）。"""
    return status()["state"] == "ready"


def pick_compute_type() -> str:
    import ctranslate2
    types = ctranslate2.get_supported_compute_types("cuda")
    for t in ("float16", "int8_float16", "int8_float32", "float32"):
        if t in types:
            return t
    return "float32"


def cuda_device_count() -> int:
    import ctranslate2
    return int(ctranslate2.get_cuda_device_count())


# ---------------- 下载（按字节范围只取需要的 DLL） ----------------

class Cancelled(Exception):
    pass


def _get(url: str, start: int, end: int, timeout=30):
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}", "User-Agent": UA})
    r = urllib.request.urlopen(req, timeout=timeout)
    if r.status != 206:
        r.close()
        raise RuntimeError(f"镜像不支持按范围下载（HTTP {r.status}）")
    return r


def _read_range(url: str, start: int, end: int) -> bytes:
    with _get(url, start, end) as r:
        return r.read()


def _central_dir(url: str, size: int) -> dict:
    """读 zip 末尾的中央目录，返回 {文件名: (本地头偏移, 压缩大小, 原始大小, crc, 压缩方式)}。"""
    tail_len = min(size, 65536 + 22)
    tail = _read_range(url, size - tail_len, size - 1)
    i = tail.rfind(b"PK\x05\x06")
    if i < 0:
        raise RuntimeError("找不到 zip 目录")
    cd_size, cd_off = struct.unpack("<II", tail[i + 12:i + 20])
    if cd_off == 0xFFFFFFFF:      # zip64
        j = tail.rfind(b"PK\x06\x06")
        cd_size, cd_off = struct.unpack("<QQ", tail[j + 40:j + 56])
    cd = _read_range(url, cd_off, cd_off + cd_size - 1)
    out, p = {}, 0
    while p + 46 <= len(cd) and cd[p:p + 4] == b"PK\x01\x02":
        (method, _t, _d, crc, csize, usize, nlen, xlen, clen) = struct.unpack("<HHHIIIHHH", cd[p + 10:p + 34])
        off = struct.unpack("<I", cd[p + 42:p + 46])[0]
        name = cd[p + 46:p + 46 + nlen].decode("utf-8", "replace")
        extra = cd[p + 46 + nlen:p + 46 + nlen + xlen]
        if 0xFFFFFFFF in (csize, usize, off):      # zip64 扩展字段
            q = 0
            while q + 4 <= len(extra):
                hid, hl = struct.unpack("<HH", extra[q:q + 4])
                if hid == 1:
                    vals = list(struct.unpack("<" + "Q" * (hl // 8), extra[q + 4:q + 4 + hl]))
                    if usize == 0xFFFFFFFF:
                        usize = vals.pop(0)
                    if csize == 0xFFFFFFFF:
                        csize = vals.pop(0)
                    if off == 0xFFFFFFFF:
                        off = vals.pop(0)
                q += 4 + hl
        out[name] = (off, csize, usize, crc, method)
        p += 46 + nlen + xlen + clen
    return out


def _data_start(url: str, local_off: int) -> int:
    h = _read_range(url, local_off, local_off + 29)
    if h[:4] != b"PK\x03\x04":
        raise RuntimeError("zip 本地头不对")
    nlen, xlen = struct.unpack("<HH", h[26:30])
    return local_off + 30 + nlen + xlen


def _first_mirror(path: str, size: int, skip=()) -> str:
    last = None
    for m in MIRRORS:
        if m in skip:
            continue
        url = m + path
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                n = int(r.headers.get("Content-Length") or 0)
            if n == size:
                return m
            last = f"{m} 大小不对 {n}"
        except Exception as ex:  # noqa
            last = f"{m} {ex}"
    raise RuntimeError(f"所有镜像都连不上（最后一个：{last}）")


def plan(wheels=None) -> list:
    """[(wheel, member, 压缩大小)]，不联网，用于估算。"""
    return [(w, m) for w in (wheels or WHEELS) for m in w["members"]]


def download(progress=None, cancel=None, wheels=None, dest: Path | None = None) -> Path:
    """下载并解出运行库。progress(已下字节, 总字节, 文字)；cancel() 返回 True 就停（下次接着下）。"""
    wheels = wheels or WHEELS
    dest = Path(dest or cuda_dir())
    dest.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda a, b, t="": None)
    stop = cancel or (lambda: False)
    try:
        man = json.loads((dest / MANIFEST).read_text("utf-8"))
    except Exception:
        man = {}
    # 1) 先把每个轮子的目录读出来，算总量
    jobs, bad = [], set()
    for w in wheels:
        mirror = _first_mirror(w["path"], w["size"])
        cd = None
        for _try in range(len(MIRRORS)):
            try:
                cd = _central_dir(mirror + w["path"], w["size"])
                break
            except Exception:
                bad.add(mirror)
                mirror = _first_mirror(w["path"], w["size"], skip=bad)
        if cd is None:
            raise RuntimeError(f"{w['name']} 读不到目录")
        for m in w["members"]:
            if m not in cd:
                raise RuntimeError(f"{w['name']} 里没有 {m}")
            jobs.append((w, m, cd[m]))
    total = sum(j[2][1] for j in jobs)
    done_bytes = 0
    t0, last_say = time.time(), 0.0
    # 2) 逐个 DLL：压缩数据按范围下到 .part（可续传），再解压 + CRC 校验
    for w, m, (off, csize, usize, crc, method) in jobs:
        fn = m.rsplit("/", 1)[-1]
        out = dest / fn
        if out.is_file() and out.stat().st_size == usize and man.get(fn) == usize:
            done_bytes += csize
            continue
        part = dest / (fn + ".part")
        mirror = _first_mirror(w["path"], w["size"], skip=bad)
        url = mirror + w["path"]
        start = _data_start(url, off)
        fails = 0
        while True:
            have = part.stat().st_size if part.exists() else 0
            if have > csize:
                part.unlink(); have = 0
            if have == csize:
                break
            if stop():
                raise Cancelled()
            try:
                with _get(url, start + have, start + csize - 1, timeout=30) as r, open(part, "ab") as fh:
                    while True:
                        if stop():
                            raise Cancelled()
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
                        have += len(chunk)
                        now = time.time()
                        if now - last_say > 0.5:
                            last_say = now
                            got = done_bytes + have
                            spd = got / max(1e-3, now - t0) / 1e6
                            say(got, total, f"下载 {w['name']} · {fn}：{got/1e6:.0f} / {total/1e6:.0f} MB"
                                            f"（{spd:.1f} MB/s，{mirror.split('/')[2]}）")
                fails = 0
            except Cancelled:
                raise
            except Exception as ex:  # noqa  断了：换个镜像接着下
                fails += 1
                if fails > 2 * len(MIRRORS):
                    raise RuntimeError(f"下载 {fn} 一直失败：{ex}") from ex
                bad.add(mirror)
                try:
                    mirror = _first_mirror(w["path"], w["size"], skip=bad)
                except Exception:
                    bad.clear()
                    time.sleep(3)
                    mirror = _first_mirror(w["path"], w["size"])
                url = mirror + w["path"]
                start = _data_start(url, off)
                say(done_bytes + have, total, f"网络断了，换 {mirror.split('/')[2]} 接着下…")
        say(done_bytes + csize, total, f"解压校验 {fn}…")
        tmp = dest / (fn + ".tmp")
        c = 0
        n = 0
        dz = zlib.decompressobj(-15) if method == 8 else None
        with open(part, "rb") as src, open(tmp, "wb") as dst:
            while True:
                b = src.read(4 << 20)
                if not b:
                    break
                b = dz.decompress(b) if dz else b
                c = zlib.crc32(b, c); n += len(b); dst.write(b)
            if dz:
                b = dz.flush(); c = zlib.crc32(b, c); n += len(b); dst.write(b)
        if n != usize or (c & 0xFFFFFFFF) != crc:
            tmp.unlink(missing_ok=True); part.unlink(missing_ok=True)
            raise RuntimeError(f"{fn} 校验失败（文件坏了，已删掉，重新下载即可）")
        os.replace(tmp, out)
        part.unlink(missing_ok=True)
        man[fn] = usize
        (dest / MANIFEST).write_text(json.dumps(man, ensure_ascii=False, indent=1), "utf-8")
        done_bytes += csize
    say(total, total, "CUDA 运行库已就绪")
    return dest


def remove_runtime():
    import shutil
    shutil.rmtree(cuda_dir(), ignore_errors=True)


class DownloadThread(threading.Thread):
    """不依赖 Qt 的后台下载（界面那边自己轮询 state）。"""
    def __init__(self):
        super().__init__(daemon=True)
        self.got = 0; self.total = 0; self.text = "准备下载…"
        self.error = ""; self.finished = False; self.cancelled = False
        self._stop = threading.Event()

    def cancel(self):
        self._stop.set()

    def run(self):
        def p(a, b, t=""):
            self.got, self.total, self.text = a, b, t
        try:
            download(p, self._stop.is_set)
        except Cancelled:
            self.cancelled = True
            self.text = "已暂停，下次点下载会接着下"
        except Exception as ex:  # noqa
            self.error = str(ex)
        finally:
            self.finished = True


# ---------------- 自检用 ----------------

def file_version(path: str) -> str:
    """Windows DLL 的文件版本号（如 9.10.2.21）。"""
    import ctypes
    from ctypes import wintypes
    ver = ctypes.windll.version
    n = ver.GetFileVersionInfoSizeW(path, None)
    if not n:
        return ""
    buf = ctypes.create_string_buffer(n)
    ver.GetFileVersionInfoW(path, 0, n, buf)
    p = ctypes.c_void_p(); ln = wintypes.UINT()
    ver.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(ln))
    ms, ls = struct.unpack("<II", ctypes.string_at(p.value + 8, 8))
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def bundled_cudnn_version() -> str:
    import ctranslate2
    return file_version(str(Path(ctranslate2.__file__).parent / "cudnn64_9.dll"))


CUDNN_VERSION = "9.10.2.21"


def selftest() -> str:
    if not is_windows():
        return "非 Windows，跳过：" + status()["text"]
    import tempfile
    import ctypes
    bv = bundled_cudnn_version()
    if bv != CUDNN_VERSION:
        raise RuntimeError(f"ctranslate2 自带 cudnn64_9.dll 是 {bv}，下载的是 {CUDNN_VERSION}，版本对不上")
    n = cuda_device_count()
    # 两个轮子的目录都读得到、要的 DLL 都在
    for w in WHEELS:
        m = _first_mirror(w["path"], w["size"])
        cd = _central_dir(m + w["path"], w["size"])
        miss = [x for x in w["members"] if x not in cd]
        if miss:
            raise RuntimeError(f"{w['name']} 缺 {miss}")
    # 真下一个最小的（cudnn_graph，约 1 MB）→ 解压校验 → 能被 Windows 加载
    w = dict(WHEELS[1]); w["members"] = ["nvidia/cudnn/bin/cudnn_graph64_9.dll"]
    d = Path(tempfile.mkdtemp()) / "cuda"
    download(wheels=[w], dest=d)
    ctypes.WinDLL(str(d / "cudnn_graph64_9.dll"))
    return f"cudnn 版本对上 {bv}；CUDA 设备 {n} 个；镜像目录齐；样例 DLL 下载+校验+加载通过；{status()['text']}"
