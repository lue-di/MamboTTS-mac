# MamboTTS（macOS 版）

基于 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) 的曼波音色桌面配音客户端：输入文案，本地合成语音，无需联网即可使用。

> **出处声明**：本项目是 [Tsukimisaka/MamboTTS](https://github.com/Tsukimisaka/MamboTTS)（MIT）的 macOS 适配衍生版本。原项目的客户端、界面与引擎调用契约由原作者及贡献者完成；本版本在其基础上移除了 Windows 专属内容（整合包安装、`.bat` 脚本、Premiere Pro 插件），并补充了 macOS 安装流程。详见文末「致谢与出处」。

## 功能

- **文案合成**：在文本框输入文案，调用本地 GPT-SoVITS 引擎合成 WAV；超长文本自动按标点切分。
- **语速调节**：0.1x–3.0x 连续滑条（引擎原生变速，不变调）。
- **播放试听**：内置播放器，支持进度条拖动。
- **历史记录**：合成历史跨重启保留，支持回放 / 另存 / 删除。
- **运行日志**：主界面实时显示引擎状态与日志。
- **文件命名**：默认取文案前 20 字作文件名，可自定义文件名与保存目录。
- **深浅主题**：一键切换，选择自动记忆。
- **单一入口**：双击 `MamboTTS.command`，自动完成依赖安装、引擎下载与启动。

## 系统要求

| 项目 | 要求 |
| --- | --- |
| 系统 | macOS（Apple Silicon / Intel） |
| Python | 3.10–3.12（推荐 3.11；Apple Silicon 也可用 3.13），需带 tkinter |
| 工具 | Xcode 命令行工具（部分依赖需要编译） |
| 网络 | 首次运行需联网，下载约 3GB（源码 + 依赖 + 模型 + 曼波权重）；合成阶段可离线 |
| 磁盘 | 约 8GB |
| 路径 | 建议使用全英文路径 |

> macOS 上推理使用 CPU（与上游 GPT-SoVITS 一致，MPS 存在音质问题），合成速度较慢。

## 快速开始

1. 安装 Python 3.11：从 [python.org](https://www.python.org/downloads/macos/) 下载，或 `brew install python@3.11 python-tk@3.11`。
2. 安装 Xcode 命令行工具：`xcode-select --install`（缺失时安装器也会自动弹出系统安装窗口）。
3. 克隆本仓库，在访达中双击 `MamboTTS.command`。
   - 提示无权限：终端执行 `chmod +x MamboTTS.command`。
   - 被 Gatekeeper 拦截：右键 →「打开」。
4. 首次运行会自动完成：创建 `.venv` 并安装客户端依赖 → 下载 GPT-SoVITS 源码 → 创建 `GPT-SoVITS/runtime` 并安装推理依赖 → 下载预训练模型 → 下载曼波音色权重到 `models/`。
5. 进入主界面后输入文案，点击「合成配音」。首次加载引擎约 10–30 秒。

安装器支持多源自动测速、断点续传与失败自动换源，中断后重新双击即可继续。

## 曼波音色权重

权重文件放在 `models/` 目录：

| 文件 | 说明 |
| --- | --- |
| `manbo_e8_s168.pth` | SoVITS 模型 |
| `manbo-e10.ckpt` | GPT 模型 |
| `refer.wav` | 参考音频（随仓库提供） |

两个权重体积较大，未纳入 Git 仓库。缺失时安装器会自动从 [原项目 Release](https://github.com/Tsukimisaka/MamboTTS/releases) 的 `MamboTTS-v1.2.1-full.zip` 下载并仅提取 `models/` 下的文件。自动下载失败时，可手动下载该 zip，把其中 `models/` 下的两个权重文件放入本项目 `models/` 目录。

也可以换成自己的 GPT-SoVITS 音色：放入对应的 `.pth` / `.ckpt`，修改 `engine_contract.py` 中的 `SOVITS_MODEL` / `GPT_MODEL`，并让 `DEFAULT_REF_TEXT` 与 `refer.wav` 内容保持一致。

## 常见问题

<details>
<summary>提示「引擎文件不完整，无法启动」？</summary>

说明 `models/` 里缺少上述两个权重文件，参见「曼波音色权重」。
</details>

<details>
<summary>安装很慢或看起来卡住？</summary>

安装器会对 ModelScope / hf-mirror / HuggingFace 等源测速并选最快的，期间日志会显示进度；单源失败会自动切换，重新双击可断点续传。
</details>

<details>
<summary>一直显示引擎启动中？</summary>

首次加载约 10–30 秒属正常。超时可点击「重启引擎」，并在运行日志中查看原因。
</details>

<details>
<summary>端口 9880 被占用？</summary>

引擎固定监听 `127.0.0.1:9880`。若已有 GPT-SoVITS 在运行则无需重复启动，否则请关闭占用该端口的程序。
</details>

## 工作原理

MamboTTS 是 GPT-SoVITS 的图形客户端与启动器。本仓库**不包含、也不修改** GPT-SoVITS 本体：引擎在首次运行时下载上游源码（固定为 `20250606v2pro` 版本）并安装推理依赖，客户端通过本地 HTTP API（`127.0.0.1:9880`）调用，接口契约见 `engine_contract.py`。

## 高级配置

- **自建 / 远程引擎**：主界面「引擎接口」可填写自定义服务地址（对应 `config.json` 的 `api_url`）。
- **配置文件**：`config.json` 由程序自动生成，模板见 `config.example.json`。

## 项目结构

| 文件 | 职责 |
| --- | --- |
| `MamboTTS.command` | 一键启动入口 |
| `bootstrap.py` | 引导窗口：创建 venv、安装客户端依赖 |
| `launcher.py` | 检测引擎安装状态、安装界面、拉起主界面 |
| `install_engine_macos.py` | 引擎安装：源码 + 推理依赖 + 预训练模型 + 曼波权重 |
| `download_utils.py` | 多源测速与断点续传下载 |
| `run_engine.py` | 引擎启动与预检 |
| `engine_contract.py` | 引擎调用契约常量 |
| `inference.py` | 引擎 HTTP 客户端 |
| `app.py` | 主界面：合成 / 播放器 / 历史 / 日志 / 主题 |
| `theme.py` · `history_*.py` · `config_store.py` · `file_naming.py` | 主题、历史记录、配置、文件命名 |

## 免责声明

本软件仅供个人学习、技术研究与学术交流使用。用户需自行确保参考音频与生成内容的权利合规，并承担相应使用责任；严禁将生成的声音用于诈骗、仿冒、侵犯他人声音权 / 肖像权等非法用途。

## 致谢与出处

- **[Tsukimisaka/MamboTTS](https://github.com/Tsukimisaka/MamboTTS)**：本项目的上游，客户端、界面、引擎契约与曼波音色整合均源自该项目（MIT，Copyright (c) 2026 Tsukimisaka）。原仓库的其他贡献者包括 sickn33、panjiangle、2710165659。
- **[RVC-Boss/GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)**：语音合成引擎（MIT，Copyright (c) 2024 RVC-Boss）。预训练模型来自 [lj1995/GPT-SoVITS](https://huggingface.co/lj1995/GPT-SoVITS) 与 [XXXXRT/GPT-SoVITS-Pretrained](https://www.modelscope.cn/models/XXXXRT/GPT-SoVITS-Pretrained)。
- 曼波音色权重沿用原项目 Release 中的发布版本；使用时请遵守其来源方的使用约定。

## 许可

本项目基于 [MIT](LICENSE) 许可开源，并保留原作者版权声明；GPT-SoVITS 引擎本体及其模型遵循其上游许可。
