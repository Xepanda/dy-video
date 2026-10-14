# 常用工具与运行环境安装配置指南 (Tools Setup Guide)

为了保证您在任何新电脑（Windows / Mac / Linux）上拉取本仓库后，都能在 5~10 分钟内快速准备好所有环境并无缝运行，请参照以下配置清单。

---

## 一、 核心重器安装（大工具）

### 1. FFmpeg（多媒体剪辑与滤镜合成核心引擎）
FFmpeg 是整套流水线的音视频编解码与滤镜渲染底座，**必须支持 `libass` 滤镜**。

- **Windows 安装方式**：
  1. 访问官方推荐下载源：[gyan.dev FFmpeg Builds](https://www.gyan.dev/ffmpeg/builds/)
  2. 下载 `ffmpeg-release-full.7z`（或 `ffmpeg-git-full.7z`）；
  3. 解压到本地目录（如 `C:\Program Files\ffmpeg` 或 `D:\tools\ffmpeg`）；
  4. 将解压目录下的 `bin` 文件夹绝对路径添加到系统环境变量 `Path` 中；
  5. 打开终端输入 `ffmpeg -version`，能正常输出版本号即表示安装成功。
- **Mac 安装方式**：
  ```bash
  brew install ffmpeg --with-libass
  ```
- **Linux (Ubuntu/Debian) 安装方式**：
  ```bash
  sudo apt update && sudo apt install -y ffmpeg libass-dev
  ```

---

### 2. Python 运行环境与 GPU 加速（可选）
推荐使用 **Python 3.10 ~ 3.11**。

- **Python 下载安装**：
  - 官网下载：[Python 官方下载页](https://www.python.org/downloads/)
  - 安装时勾选：`Add python.exe to PATH`。
- **依赖库一键安装**：
  在项目根目录下执行：
  ```bash
  pip install -r requirements.txt
  ```
- **Faster-Whisper GPU 加速配置（如有 NVIDIA 显卡）**：
  - 如果需要显卡极速转录，需安装 CUDA 11.8/12.x 及 cuDNN；
  - 若无显卡，代码会自动降级为 CPU（float32/int8）运行，兼容性 100%。

---

## 二、 开源辅助工具与在线协作平台推荐

| 工具名称 | 类型 | 用途说明 | 获取方式 / 官网链接 |
| :--- | :--- | :--- | :--- |
| **Aegisub** | 本地桌面客户端 | 专业的 `.ass` 字幕可视化微调与样式预览工具 | [Aegisub 官网](http://www.aegisub.org/) |
| **CapCut / 剪映桌面版** | 本地桌面客户端 | 用于人工最后快速审核素材或调色 | [剪映官网](https://www.capcut.cn/) |
| **LosslessCut** | 本地绿色小工具 | 基于 FFmpeg 的极速无损素材预剪切工具（秒级定位） | [GitHub Releases](https://github.com/mifi/lossless-cut/releases) |
| **Canva / 稿定设计** | 网页端在线平台 | 用于寻找更多抖音电商爆款封面排版模板灵感 | [稿定设计](https://www.gaoding.com/) / [Canva](https://www.canva.cn/) |

---

## 三、 系统字体配置说明
本流水线在合成贴纸、封面与标题时，默认调用 Windows 系统自带中文字体：
- **粗黑体**：`C:/Windows/Fonts/simhei.ttf`（黑体，用于主标题与强视觉冲击字）
- **微软雅黑粗体**：`C:/Windows/Fonts/msyhbd.ttc`（用于副标题与说明性文字）

*注：若迁移至 Linux 或 macOS 系统，只需在 `src/pipeline.py` 中将字体路径替换为本地的思源黑体（Source Han Sans）或 PingFang 路径即可。*
