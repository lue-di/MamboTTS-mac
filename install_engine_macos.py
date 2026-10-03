"""
macOS 引擎安装

GPT-SoVITS 官方没有 macOS 整合包，这里采用与上游发布版本一致的「源码 + 独立 venv + 预训练模型」方案：

    GPT-SoVITS/                 ← 上游源码（api.py 位于根目录，与整合包结构一致）
    GPT-SoVITS/runtime/         ← 推理专用 venv（runtime/bin/python）
    GPT-SoVITS/GPT_SoVITS/pretrained_models/  ← BERT / HuBERT 等推理必需模型
    GPT-SoVITS/GPT_SoVITS/text/G2PWModel/     ← 中文多音字模型

只下载曼波推理用得到的部分（约 1.6GB 模型 + 约 1GB 依赖），不拉 4.5GB 的全量预训练包。
推理设备沿用上游策略：macOS 一律 CPU（上游因音质问题弃用了 MPS）。
"""
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile

from engine_contract import DEFAULT_REF_TEXT, SOVITS_MODEL, GPT_MODEL, REF_WAV

# 固定上游发布版本，保证 api.py 行为一致
GSV_TAG = "20250606v2pro"
SOURCE_URLS = [
    f"https://codeload.github.com/RVC-Boss/GPT-SoVITS/zip/refs/tags/{GSV_TAG}",
    # GitHub 在国内常不可达：公共加速代理兜底
    f"https://ghfast.top/https://github.com/RVC-Boss/GPT-SoVITS/archive/refs/tags/{GSV_TAG}.zip",
]

# 预训练模型逐文件下载源（{path} 为相对 pretrained_models/ 的路径）。
# ModelScope 在国内最快排第一；顺序最终由测速决定。
PRETRAINED_SOURCES = [
    "https://www.modelscope.cn/models/XXXXRT/GPT-SoVITS-Pretrained/resolve/master/pretrained_models/{path}",
    "https://hf-mirror.com/lj1995/GPT-SoVITS/resolve/main/{path}",
    "https://huggingface.co/lj1995/GPT-SoVITS/resolve/main/{path}",
]
# 压缩包资源（G2PW / nltk）下载源，{file} 为文件名
ARCHIVE_SOURCES = [
    "https://www.modelscope.cn/models/XXXXRT/GPT-SoVITS-Pretrained/resolve/master/{file}",
    "https://hf-mirror.com/XXXXRT/GPT-SoVITS-Pretrained/resolve/main/{file}",
    "https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/main/{file}",
]

# (相对 pretrained_models 的路径, 字节数, 是否必需)。大小取自 ModelScope 仓库 API。
PRETRAINED_FILES = [
    ("chinese-roberta-wwm-ext-large/config.json", 963, True),
    ("chinese-roberta-wwm-ext-large/tokenizer.json", 268962, True),
    ("chinese-roberta-wwm-ext-large/pytorch_model.bin", 651225145, True),
    ("chinese-hubert-base/config.json", 1449, True),
    ("chinese-hubert-base/preprocessor_config.json", 212, True),
    ("chinese-hubert-base/pytorch_model.bin", 188811417, True),
    # 语种识别大模型：仅 ModelScope 有，缺失时 fast_langdetect 退回内置小模型，不算致命
    ("fast_langdetect/lid.176.bin", 131266198, False),
]
# (文件名, 字节数, 解压目标（相对 GPT-SoVITS 根）, 解压后应存在的目录)
G2PW_ARCHIVE = ("G2PWModel.zip", 588856634, "GPT_SoVITS/text", "GPT_SoVITS/text/G2PWModel")
NLTK_ARCHIVE = ("nltk_data.zip", 9924448, None, None)  # 解压到 runtime venv 的 sys.prefix

TOTAL_DOWNLOAD_BYTES = (sum(s for _, s, _ in PRETRAINED_FILES)
                        + G2PW_ARCHIVE[1] + NLTK_ARCHIVE[1])
# 依赖（torch 等）约 1GB，加上源码/模型解压产物与余量
DISK_NEED_BYTES = 8 * 1024 ** 3

# 曼波音色权重（SoVITS / GPT）：随 GitHub Release 的 full.zip 发布，缺失时自动下载并只提取 models/ 下三个文件。
MANBO_RELEASE_ZIP = ("https://github.com/Tsukimisaka/MamboTTS/releases/download/v1.2.1/MamboTTS-v1.2.1-full.zip",
                     "MamboTTS-v1.2.1-full.zip", 223155406)
MANBO_MODEL_FILES = (SOVITS_MODEL, GPT_MODEL, REF_WAV)

PIP_MIRROR = "https://mirrors.aliyun.com/pypi/simple/"

# 阶段名与权重（进度条用）
STAGES = [
    ("检查系统环境", 0.03),
    ("下载引擎源码", 0.07),
    ("创建运行环境", 0.05),
    ("安装推理依赖", 0.35),
    ("下载预训练模型", 0.45),
    ("收尾配置", 0.05),
]


def _stage_base(idx):
    return sum(w for _, w in STAGES[:idx])


class _Canceled(Exception):
    pass


# ============================================================
# 工具函数
# ============================================================

def _probe_python(cmd):
    """返回 (major, minor, machine)；不可用返回 None"""
    try:
        out = subprocess.check_output(
            [cmd, "-c", "import sys,platform;print(sys.version_info[0],sys.version_info[1],platform.machine())"],
            stderr=subprocess.DEVNULL, timeout=10,
        ).decode("utf-8", "replace").split()
        return int(out[0]), int(out[1]), out[2]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _torch_spec(machine):
    """按 CPU 架构选 torch 版本：PyTorch 2.3 起不再发布 Intel Mac wheel。"""
    if machine == "arm64":
        return ["torch==2.6.0", "torchaudio==2.6.0"], ((3, 10), (3, 13))
    return ["torch==2.2.2", "torchaudio==2.2.2"], ((3, 10), (3, 12))


def find_runtime_python():
    """为引擎 venv 挑选一个版本合适的 Python（优先 3.11，最兼容上游依赖）。

    返回 (路径, (major, minor), machine) 或 None。"""
    cands = []
    base = getattr(sys, "_base_executable", None)
    for c in (base, sys.executable):
        if c:
            cands.append(c)
    for ver in ("3.11", "3.12", "3.10", "3.13"):
        cands += [
            shutil.which(f"python{ver}"),
            f"/opt/homebrew/bin/python{ver}",
            f"/usr/local/bin/python{ver}",
            f"/Library/Frameworks/Python.framework/Versions/{ver}/bin/python{ver}",
        ]
    cands.append(shutil.which("python3"))

    seen, found = set(), []
    for c in cands:
        if not c or not os.path.exists(c):
            continue
        real = os.path.realpath(c)
        if real in seen:
            continue
        seen.add(real)
        info = _probe_python(c)
        if not info:
            continue
        major, minor, machine = info
        _, (lo, hi) = _torch_spec(machine)
        if lo <= (major, minor) <= hi:
            found.append((c, (major, minor), machine))
    if not found:
        return None
    pref = [(3, 11), (3, 12), (3, 10), (3, 13)]
    found.sort(key=lambda x: pref.index(x[1]) if x[1] in pref else 99)
    return found[0]


def _has_clt():
    """Xcode 命令行工具：jieba_fast 等依赖只有源码包，需要 clang 编译。"""
    try:
        return subprocess.run(["xcode-select", "-p"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run_streaming(cmd, log_cb, cancel_cb, on_line=None, cwd=None):
    """运行子进程并逐行转发输出；取消时终止进程。返回退出码，取消抛 _Canceled。"""
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True, encoding="utf-8", errors="replace", bufsize=1,
        env={**os.environ, "PYTHONUTF8": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )
    stop = threading.Event()

    def _watch():
        # pip 解析依赖/编译时可能长时间无输出，单靠逐行检查取消会卡住
        while not stop.wait(0.5):
            if cancel_cb():
                try:
                    proc.terminate()
                except OSError:
                    pass
                return

    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            if on_line:
                on_line(line)
            # 过滤 pip 的进度条行，避免日志爆炸
            if len(line) < 200 and "━" not in line:
                log_cb(line)
        rc = proc.wait()
    finally:
        stop.set()
    if cancel_cb():
        raise _Canceled()
    return rc


def _download_unknown_size(url, dest, log_cb, cancel_cb):
    """下载未知大小的小文件（GitHub 源码 zip 为 chunked 响应，无 Content-Length）。"""
    import requests
    tmp = dest + ".part"
    try:
        with requests.get(url, stream=True, timeout=(15, 30)) as r:
            if r.status_code != 200:
                log_cb(f"[警告] 源返回 HTTP {r.status_code}：{url[:80]}")
                return False
            done, last = 0, time.time()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if cancel_cb():
                        raise _Canceled()
                    f.write(chunk)
                    done += len(chunk)
                    if time.time() - last >= 3:
                        last = time.time()
                        log_cb(f"[状态] 已下载 {done / (1024 ** 2):.1f}MB")
        os.replace(tmp, dest)
        return True
    except _Canceled:
        raise
    except Exception as e:
        log_cb(f"[警告] 下载失败（{type(e).__name__}）：{str(e)[:100]}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _download_with_fallback(urls, dest, size, log_cb, cancel_cb, progress_cb, base, weight):
    """按顺序尝试多个源下载已知大小文件。"""
    from download_utils import download_file_resumable
    if os.path.exists(dest) and os.path.getsize(dest) == size:
        return True
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    for url in urls:
        if cancel_cb():
            raise _Canceled()
        if download_file_resumable(url, dest, size, progress_cb, log_cb, cancel_cb,
                                   stage_base=base, stage_weight=weight):
            return True
        if cancel_cb():
            raise _Canceled()
    return False


# ============================================================
# 安装主流程
# ============================================================

def install(progress_cb, log_cb, state_cb, cancel_cb, base_dir):
    """返回 (success, message)，返回值约定：(是否成功, 提示信息)。"""
    try:
        return _install(progress_cb, log_cb, state_cb, cancel_cb, base_dir)
    except _Canceled:
        return False, "安装已取消，已下载/已安装的部分会保留，重试将继续。"
    except Exception as e:
        log_cb(f"[错误] 安装过程中发生异常: {type(e).__name__}: {e}")
        return False, f"安装过程中发生异常: {e}"


def _install(progress_cb, log_cb, state_cb, cancel_cb, base_dir):
    gsv_dir = os.path.join(base_dir, "GPT-SoVITS")
    temp_dir = os.path.join(base_dir, "engine_temp")
    runtime_dir = os.path.join(gsv_dir, "runtime")
    runtime_py = os.path.join(runtime_dir, "bin", "python")
    os.makedirs(temp_dir, exist_ok=True)

    log_cb("===================================================")
    log_cb("          MamboTTS 引擎一键安装程序（macOS）")
    log_cb("===================================================")

    # ===== 阶段 0：系统环境 =====
    state_cb(STAGES[0][0])
    try:
        free = shutil.disk_usage(base_dir).free
        if free < DISK_NEED_BYTES:
            return False, (f"磁盘剩余空间不足：需要约 {DISK_NEED_BYTES / 1024 ** 3:.0f}GB，"
                           f"当前仅剩 {free / 1024 ** 3:.1f}GB。")
    except OSError:
        pass

    if not _has_clt():
        log_cb("[错误] 未检测到 Xcode 命令行工具（编译部分依赖需要），已尝试弹出系统安装窗口。")
        try:
            subprocess.Popen(["xcode-select", "--install"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
        return False, ("缺少 Xcode 命令行工具。\n请在弹出的系统窗口中点击「安装」，"
                       "完成后重新运行安装；若未弹窗，可在终端执行：xcode-select --install")

    existing = _probe_python(runtime_py) if os.path.exists(runtime_py) else None
    if existing:
        major, minor, machine = existing
        rt = (runtime_py, (major, minor), machine)
        log_cb(f"[状态] 复用已有运行环境：Python {major}.{minor}（{machine}）")
    else:
        rt = find_runtime_python()
        if not rt:
            machine = platform.machine()
            hi = "3.13" if machine == "arm64" else "3.12"
            return False, (f"未找到可用于引擎的 Python（需要 3.10–{hi}）。\n"
                           "请安装 Python 3.11 后重试，例如：brew install python@3.11\n"
                           "或从 https://www.python.org/downloads/macos/ 下载安装。")
        log_cb(f"[状态] 引擎将使用 Python {rt[1][0]}.{rt[1][1]}（{rt[2]}）：{rt[0]}")
    progress_cb(_stage_base(1))

    # ===== 阶段 1：下载上游源码 =====
    state_cb(STAGES[1][0])
    if os.path.exists(os.path.join(gsv_dir, "api.py")):
        log_cb("[状态] 检测到已有引擎源码，跳过下载。")
    else:
        zip_path = os.path.join(temp_dir, f"GPT-SoVITS-{GSV_TAG}.zip")
        ok = False
        for url in SOURCE_URLS:
            log_cb(f"[状态] 正在下载 GPT-SoVITS 源码（{GSV_TAG}）：{url[:80]}")
            if _download_unknown_size(url, zip_path, log_cb, cancel_cb):
                try:
                    with zipfile.ZipFile(zip_path) as zf:
                        bad = zf.testzip()
                    ok = bad is None
                except zipfile.BadZipFile:
                    ok = False
                if ok:
                    break
                log_cb("[警告] 源码压缩包损坏，换下一个源...")
        if not ok:
            return False, ("引擎源码下载失败（GitHub 及加速代理均不可达）。\n"
                           f"可手动下载 https://github.com/RVC-Boss/GPT-SoVITS/archive/refs/tags/{GSV_TAG}.zip\n"
                           f"重命名为 GPT-SoVITS-{GSV_TAG}.zip 放入 {temp_dir} 后重试。")
        log_cb("[状态] 正在解压源码...")
        unpack = os.path.join(temp_dir, "src_unpack")
        shutil.rmtree(unpack, ignore_errors=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(unpack)
        entries = os.listdir(unpack)
        src_root = os.path.join(unpack, entries[0]) if len(entries) == 1 else unpack
        os.makedirs(gsv_dir, exist_ok=True)
        for item in os.listdir(src_root):
            dst = os.path.join(gsv_dir, item)
            if os.path.exists(dst):
                # 保留已有运行环境 / 已下载模型，只补齐源码
                continue
            shutil.move(os.path.join(src_root, item), dst)
        shutil.rmtree(unpack, ignore_errors=True)
        try:
            os.remove(zip_path)
        except OSError:
            pass
        log_cb("[状态] 源码就绪。")
    progress_cb(_stage_base(2))

    # ===== 阶段 2：创建 venv =====
    state_cb(STAGES[2][0])
    if os.path.exists(runtime_py):
        log_cb("[状态] 运行环境已存在，跳过创建。")
    else:
        log_cb("[状态] 正在创建引擎运行环境 GPT-SoVITS/runtime ...")
        rc = _run_streaming([rt[0], "-m", "venv", runtime_dir], log_cb, cancel_cb)
        if rc != 0 or not os.path.exists(runtime_py):
            shutil.rmtree(runtime_dir, ignore_errors=True)
            return False, f"创建运行环境失败（退出码 {rc}），请确认 Python 安装完整。"
    progress_cb(_stage_base(3))

    # ===== 阶段 3：pip 安装依赖 =====
    state_cb(STAGES[3][0])
    torch_pkgs, _ = _torch_spec(rt[2])
    req_file = os.path.join(base_dir, "requirements-engine-macos.txt")
    if not os.path.exists(req_file):
        return False, f"缺少依赖清单文件：{req_file}"
    base3, w3 = _stage_base(3), STAGES[3][1]
    seen_pkgs = [0]

    def _on_pip_line(line):
        # 无字节级进度：按「已处理包数」粗略推进（约 120 个包），不让进度条停死
        if re.match(r"\s*(Collecting|Requirement already satisfied)", line):
            seen_pkgs[0] += 1
            progress_cb(base3 + min(seen_pkgs[0] / 150.0, 0.95) * w3)

    steps = [
        ("升级 pip", ["-m", "pip", "install", "--upgrade", "pip"]),
        (f"安装 PyTorch（{' '.join(torch_pkgs)}，约 100MB）", ["-m", "pip", "install"] + torch_pkgs),
        ("安装推理依赖（首次约 3-10 分钟，含少量本地编译）", ["-m", "pip", "install", "-r", req_file]),
    ]
    for title, args in steps:
        log_cb(f"[状态] {title} ...")
        rc = _run_streaming([runtime_py] + args + ["-i", PIP_MIRROR], log_cb, cancel_cb,
                            on_line=_on_pip_line)
        if rc != 0:
            log_cb("[警告] 国内镜像安装失败，改用 PyPI 官方源重试...")
            rc = _run_streaming([runtime_py] + args, log_cb, cancel_cb, on_line=_on_pip_line)
        if rc != 0:
            return False, f"{title}失败（退出码 {rc}），详见安装日志。可检查网络后重试。"
    progress_cb(_stage_base(4))

    # ===== 阶段 4：下载预训练模型 =====
    state_cb(STAGES[4][0])
    from download_utils import benchmark_sources
    pre_dir = os.path.join(gsv_dir, "GPT_SoVITS", "pretrained_models")
    log_cb(f"[状态] 需下载推理模型约 {TOTAL_DOWNLOAD_BYTES / 1024 ** 3:.2f}GB，正在测速选择下载源...")
    probe_urls = [t.replace("{path}", "chinese-hubert-base/pytorch_model.bin")
                  for t in PRETRAINED_SOURCES]
    ordered = benchmark_sources(probe_urls, log_cb)
    pre_tpls = [PRETRAINED_SOURCES[probe_urls.index(u)] for u in ordered]
    # 压缩包源与模型源按同一主机顺序排列（同站点测速结论可复用）
    host_rank = {re.sub(r"^https://([^/]+)/.*$", r"\1", t): i for i, t in enumerate(pre_tpls)}
    arc_tpls = sorted(ARCHIVE_SOURCES,
                      key=lambda t: host_rank.get(re.sub(r"^https://([^/]+)/.*$", r"\1", t), 99))

    base4, w4 = _stage_base(4), STAGES[4][1]
    done_bytes = 0

    def _sub_progress(size):
        # 把单文件的 0~1 进度映射到整个模型阶段的加权区间
        start = base4 + w4 * done_bytes / TOTAL_DOWNLOAD_BYTES
        return start, w4 * size / TOTAL_DOWNLOAD_BYTES

    for rel, size, required in PRETRAINED_FILES:
        dest = os.path.join(pre_dir, *rel.split("/"))
        start, weight = _sub_progress(size)
        urls = [t.replace("{path}", rel) for t in pre_tpls]
        if not (os.path.exists(dest) and os.path.getsize(dest) == size):
            log_cb(f"[状态] 下载 {rel}（{size / 1024 ** 2:.1f}MB）")
        if not _download_with_fallback(urls, dest, size, log_cb, cancel_cb,
                                       progress_cb, start, weight):
            if required:
                return False, f"模型文件 {rel} 所有源均下载失败，请检查网络后重试（已下载部分会续传）。"
            log_cb(f"[提示] 可选模型 {rel} 下载失败，跳过（不影响中文合成）。")
        done_bytes += size
        progress_cb(base4 + w4 * done_bytes / TOTAL_DOWNLOAD_BYTES)

    # G2PW 中文多音字模型
    fname, size, extract_rel, check_rel = G2PW_ARCHIVE
    if os.path.isdir(os.path.join(gsv_dir, *check_rel.split("/"))):
        log_cb("[状态] G2PW 多音字模型已存在，跳过。")
    else:
        log_cb(f"[状态] 下载 G2PW 多音字模型（{size / 1024 ** 2:.0f}MB）")
        dest = os.path.join(temp_dir, fname)
        start, weight = _sub_progress(size)
        if not _download_with_fallback([t.replace("{file}", fname) for t in arc_tpls], dest, size,
                                       log_cb, cancel_cb, progress_cb, start, weight):
            return False, "G2PW 模型下载失败，请检查网络后重试（已下载部分会续传）。"
        log_cb("[状态] 解压 G2PW 模型...")
        with zipfile.ZipFile(dest) as zf:
            zf.extractall(os.path.join(gsv_dir, *extract_rel.split("/")))
        os.remove(dest)
    done_bytes += size

    # nltk 数据（英文混读 g2p_en 依赖），解压到 venv 的 sys.prefix（nltk 默认搜索路径之一）
    fname, size, _, _ = NLTK_ARCHIVE
    if os.path.isdir(os.path.join(runtime_dir, "nltk_data")):
        log_cb("[状态] nltk 数据已存在，跳过。")
    else:
        log_cb("[状态] 下载 nltk 英文数据（约 10MB）")
        dest = os.path.join(temp_dir, fname)
        start, weight = _sub_progress(size)
        if _download_with_fallback([t.replace("{file}", fname) for t in arc_tpls], dest, size,
                                   log_cb, cancel_cb, progress_cb, start, weight):
            with zipfile.ZipFile(dest) as zf:
                zf.extractall(runtime_dir)
            os.remove(dest)
        else:
            log_cb("[提示] nltk 数据下载失败，跳过（仅影响中英混读，引擎首次遇到英文时会尝试自动下载）。")
    progress_cb(_stage_base(5))

    # ===== 阶段 5：收尾 =====
    state_cb(STAGES[5][0])
    models_dir = os.path.join(base_dir, "models")
    missing = [n for n in (SOVITS_MODEL, GPT_MODEL) if not os.path.exists(os.path.join(models_dir, n))]
    if missing:
        url, zname, zsize = MANBO_RELEASE_ZIP
        log_cb(f"[状态] 缺少曼波音色权重 {', '.join(missing)}，从 GitHub Release 下载（{zsize / 1024 ** 2:.0f}MB）...")
        zdest = os.path.join(temp_dir, zname)
        if not _download_with_fallback([url, "https://ghfast.top/" + url], zdest, zsize,
                                       log_cb, cancel_cb, progress_cb, _stage_base(5), STAGES[5][1]):
            return False, ("曼波音色权重下载失败。可手动从项目 Releases 下载 full.zip，"
                           "把其中 models/ 下的 .pth 与 .ckpt 放入本项目 models/ 目录后重试。")
        os.makedirs(models_dir, exist_ok=True)
        with zipfile.ZipFile(zdest) as zf:
            for member in zf.namelist():
                name = os.path.basename(member)
                if name in MANBO_MODEL_FILES and "/models/" in "/" + member:
                    with zf.open(member) as src, open(os.path.join(models_dir, name), "wb") as dst:
                        shutil.copyfileobj(src, dst)
        os.remove(zdest)
    log_cb("[状态] 正在校验运行环境（按 api.py 的导入链逐个加载推理模块）...")
    # 与 api.py 相同的 sys.path 与工作目录：任何缺失依赖在这里暴露，而不是等引擎启动时才报
    smoke = ("import sys; sys.path[:0] = ['.', 'GPT_SoVITS']; "
             "import torch, torchaudio, librosa, soundfile, transformers, peft, fastapi, uvicorn; "
             "import feature_extractor.cnhubert, module.models, module.mel_processing, "
             "AR.models.t2s_lightning_module, process_ckpt, text.cleaner, text.LangSegmenter, "
             "text.chinese2, text.english; "
             "print('torch', torch.__version__, 'OK')")
    rc = _run_streaming([runtime_py, "-c", smoke], log_cb, cancel_cb, cwd=gsv_dir)
    if rc != 0:
        return False, "运行环境自检失败（核心依赖无法导入），详见安装日志。可重试安装修复。"

    from run_engine import engine_paths
    ep = engine_paths(base_dir)
    script = os.path.join(gsv_dir, "go-api-mambo.sh")
    with open(script, "w", encoding="utf-8") as f:
        f.write(
            "#!/bin/bash\n"
            'cd "$(dirname "$0")"\n'
            "echo Starting GPT-SoVITS API on http://127.0.0.1:9880 ...\n"
            "runtime/bin/python api.py -a 127.0.0.1 -p 9880 \\\n"
            f'  -s "{ep["sovits"]}" \\\n'
            f'  -g "{ep["gpt"]}" \\\n'
            f'  -dr "{ep["ref_wav"]}" \\\n'
            f'  -dt "{DEFAULT_REF_TEXT}" \\\n'
            "  -dl zh\n"
        )
    os.chmod(script, 0o755)

    shutil.rmtree(temp_dir, ignore_errors=True)
    with open(os.path.join(gsv_dir, ".mambo_install_ok"), "w", encoding="utf-8") as f:
        f.write(f"installed_at={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"package=macos-source-{GSV_TAG}\npython={rt[1][0]}.{rt[1][1]}-{rt[2]}\n")

    progress_cb(1.0)
    log_cb("===================================================")
    log_cb("🎉 GPT-SoVITS 本地推理引擎（macOS）安装成功！")
    log_cb("===================================================")
    log_cb(f"安装路径: {gsv_dir}")
    return True, "安装成功"
