#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "第一次运行：正在创建环境并安装依赖，需要几分钟……"
  python3 -m venv .venv
  .venv/bin/python -m pip install -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple
  .venv/bin/python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  if [ "$(uname -m)" = "arm64" ]; then
    .venv/bin/python -m pip install mlx-whisper -i https://pypi.tuna.tsinghua.edu.cn/simple
  fi
fi
.venv/bin/python run.py
