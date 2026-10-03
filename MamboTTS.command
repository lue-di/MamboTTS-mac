#!/bin/bash
# MamboTTS macOS 启动入口：在访达中双击运行
# 若提示无执行权限：在终端执行 chmod +x MamboTTS.command，或直接 bash MamboTTS.command
cd "$(dirname "$0")" || exit 1

pause_exit() {
    echo
    read -n 1 -s -r -p "按任意键关闭此窗口..."
    echo
    exit 1
}

# 选一个 ≥3.10 的 Python（macOS 自带的 /usr/bin/python3 是 3.9，不满足 PySide6 要求）
PY=""
for c in python3.12 python3.11 python3.13 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$c" >/dev/null 2>&1 && \
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        PY="$(command -v "$c")"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "[错误] 未找到 Python 3.10 或更高版本。"
    echo "请从 https://www.python.org/downloads/macos/ 安装 Python 3.11，"
    echo "或使用 Homebrew：brew install python@3.11 python-tk@3.11"
    pause_exit
fi
echo "[信息] 使用 Python: $PY"

if "$PY" -c "import tkinter" >/dev/null 2>&1; then
    # 有 tkinter：由引导窗口负责 venv + 依赖安装，并拉起主程序
    "$PY" bootstrap.py || pause_exit
else
    # 无 tkinter（Homebrew Python 未装 python-tk）：在终端里完成同样的环境准备
    echo "[信息] 当前 Python 缺少 tkinter，改为在终端中准备运行环境..."
    if [ ! -x .venv/bin/python ]; then
        echo "[状态] 正在创建虚拟环境 .venv ..."
        "$PY" -m venv .venv || pause_exit
    fi
    if ! .venv/bin/python -c "import PySide6, requests" >/dev/null 2>&1; then
        echo "[状态] 正在安装依赖 PySide6 / requests（约 1-3 分钟）..."
        .venv/bin/python -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ \
            || .venv/bin/python -m pip install -r requirements.txt \
            || pause_exit
    fi
    echo "[状态] 正在启动 MamboTTS..."
    # 脱离终端会话启动：关闭本窗口不影响主程序
    nohup .venv/bin/python launcher.py > launcher_error.log 2>&1 &
    sleep 2
fi
echo "[信息] MamboTTS 已启动，可以关闭此终端窗口。"
