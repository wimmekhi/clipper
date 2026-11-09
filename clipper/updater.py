"""一键更新：查 GitHub Releases 最新版 → 下载本平台 zip → 退出后替换自己 → 重新打开。
Win：替换 台词切片编辑器.exe；Mac：替换整个 台词切片编辑器.app。源码运行时只提示，不替换。"""
from __future__ import annotations

import json
import os
import platform
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .paths import VERSION, frozen

REPO = "wimmekhi/clipper"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE = f"https://github.com/{REPO}/releases/latest"


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _vt(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def is_newer(remote: str, local: str = VERSION) -> bool:
    return _vt(remote) > _vt(local)


def asset_suffix() -> str:
    return "macOS-arm64.zip" if platform.system() == "Darwin" else "windows-x64.zip"


def check(timeout=15) -> dict:
    """返回 {version, notes, url, size, page}；没有本平台的包时 url 为空。"""
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"clipper/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
        d = json.loads(r.read().decode("utf-8"))
    ver = d.get("tag_name", "").lstrip("v")
    url, size = "", 0
    for a in d.get("assets", []):
        if a.get("name", "").endswith(asset_suffix()):
            url, size = a["browser_download_url"], a.get("size", 0)
    return dict(version=ver, notes=d.get("body") or "", url=url, size=size, page=d.get("html_url") or PAGE)


def download(url: str, size: int = 0, progress=None, cancelled=lambda: False) -> Path:
    """下到临时目录，progress(已下载字节, 总字节)。"""
    tmp = Path(tempfile.mkdtemp(prefix="clipper-update-"))
    out = tmp / url.rsplit("/", 1)[-1]
    req = urllib.request.Request(url, headers={"User-Agent": f"clipper/{VERSION}"})
    with urllib.request.urlopen(req, timeout=60, context=_ctx()) as r, open(out, "wb") as fh:
        total = int(r.headers.get("Content-Length") or size or 0)
        got = 0
        while True:
            if cancelled():
                raise RuntimeError("已取消")
            b = r.read(1 << 20)
            if not b:
                break
            fh.write(b)
            got += len(b)
            if progress:
                progress(got, total)
    if size and out.stat().st_size != size:
        raise RuntimeError(f"下载不完整：{out.stat().st_size}/{size} 字节")
    return out


def current_target() -> Path | None:
    """要被替换的东西：Win 是 exe，Mac 是 .app。源码运行返回 None。"""
    if not frozen():
        return None
    exe = Path(sys.executable).resolve()
    if platform.system() == "Darwin":
        for p in exe.parents:
            if p.suffix == ".app":
                return p
        return None
    return exe


def prepare_and_launch(zip_path: Path) -> str:
    """解压新版、写替换脚本并在后台启动它（脚本等本程序退出后替换并重新打开）。
    调用方随后应立即退出程序。返回说明文字。"""
    target = current_target()
    if target is None:
        raise RuntimeError("源码运行的版本不能自动替换，请到 Releases 页面下载")
    work = zip_path.parent / "new"
    work.mkdir(exist_ok=True)
    pid = os.getpid()
    if platform.system() == "Darwin":
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(zip_path), str(work)], check=True)
        apps = list(work.glob("*.app")) or list(work.glob("*/*.app"))
        if not apps:
            raise RuntimeError("更新包里没找到 .app")
        new = apps[0]
        if not os.access(target.parent, os.W_OK):
            raise RuntimeError(f"没有权限写入 {target.parent}，请手动替换")
        sh = zip_path.parent / "update.sh"
        sh.write_text(f"""#!/bin/bash
while kill -0 {pid} 2>/dev/null; do sleep 0.5; done
sleep 1
OLD={_q(target)}
NEW={_q(new)}
BAK="$OLD.old"
rm -rf "$BAK"
mv "$OLD" "$BAK" && mv "$NEW" "$OLD" && rm -rf "$BAK" || {{ [ -d "$BAK" ] && [ ! -d "$OLD" ] && mv "$BAK" "$OLD"; }}
xattr -dr com.apple.quarantine "$OLD" 2>/dev/null
open "$OLD"
""", "utf-8")
        os.chmod(sh, 0o755)
        subprocess.Popen(["/bin/bash", str(sh)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return str(target)
    # Windows
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(work)
    exes = list(work.rglob("*.exe"))
    if not exes:
        raise RuntimeError("更新包里没找到 exe")
    new = exes[0]
    ps = f"""
$ErrorActionPreference = 'SilentlyContinue'
Wait-Process -Id {pid} -Timeout 120
$old = '{_ps(target)}'
$new = '{_ps(new)}'
for ($i = 0; $i -lt 90; $i++) {{
  Start-Sleep -Seconds 1
  Move-Item -LiteralPath $new -Destination $old -Force
  if (-not (Test-Path -LiteralPath $new)) {{ break }}
}}
if (Test-Path -LiteralPath $new) {{
  Add-Type -AssemblyName PresentationFramework
  [System.Windows.MessageBox]::Show("更新失败：无法替换 $old，新版在 $new")
}} else {{
  Start-Process -FilePath $old
}}
"""
    import base64
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    flags = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                      "-EncodedCommand", enc], creationflags=flags, close_fds=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(target)


def _q(p: Path) -> str:
    return "'" + str(p).replace("'", "'\\''") + "'"


def _ps(p: Path) -> str:
    return str(p).replace("'", "''")
