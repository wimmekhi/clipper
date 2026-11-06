@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  echo 第一次运行：正在创建环境并安装依赖，需要几分钟……
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple
  .venv\Scripts\python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
)
.venv\Scripts\python run.py
pause
