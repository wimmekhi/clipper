"""Apple 芯片 Mac 打包时收集 mlx-whisper（参考 isair/jarvis PR #387）。

- mlx 只列具体子模块（mlx.core / mlx.nn / mlx.utils + 包内 .py 助手），不整包 collect_all，
  避免递归导入原生扩展；
- 必带 mlx/lib/mlx.metallib（Metal 着色器，约 100+ MB），找不到就让打包失败，不出残缺的包；
- mlx_whisper 去掉可选的 torch_whisper；带上 tiktoken 及其数据；scipy/numba 交给 PyInstaller 自己的 hook。
其他平台返回空。
"""
import platform
import sys
from pathlib import Path


def collect_mlx_whisper():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return [], [], []
    from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules
    import mlx

    metallib = next((Path(r) / "lib" / "mlx.metallib" for r in mlx.__path__
                     if (Path(r) / "lib" / "mlx.metallib").is_file()), None)
    if metallib is None:
        raise RuntimeError("mlx.metallib 不见了：重新 pip install mlx 再打包")

    hidden = ["mlx", "mlx.core", "mlx.nn", "mlx.utils", "numba", "scipy.signal"]
    for r in mlx.__path__:
        hidden += [f"mlx.{p.stem}" for p in Path(r).glob("*.py") if p.stem != "__init__"]
    hidden += collect_submodules("mlx.nn")
    hidden += collect_submodules("mlx_whisper", filter=lambda n: not n.startswith("mlx_whisper.torch_whisper"))
    hidden += collect_submodules("tiktoken") + collect_submodules("tiktoken_ext")
    datas = [(str(metallib), "mlx/lib")]
    datas += collect_data_files("mlx_whisper") + collect_data_files("tiktoken")
    binaries = collect_dynamic_libs("mlx")
    print(f"[mlx_bundle] metallib {metallib.stat().st_size/1e6:.0f} MB, {len(binaries)} 个动态库, "
          f"{len(hidden)} 个模块", flush=True)
    return list(dict.fromkeys(hidden)), datas, binaries
