# 第三方组件与许可证说明（THIRD-PARTY NOTICES）

本仓库**自有源码**（Python 标准库实现 + 前端 HTML）的许可证见 [LICENSE](./LICENSE)（当前为 MIT，版权归 Kimio318 所有）。本文件仅用于厘清"自有代码 / 第三方组件"的边界，不替代各组件自身的许可证。

以下第三方组件**不在源码仓库内**（经 `.gitignore` 排除，或仅运行时以子进程调用），但**运行 / 构建 / 分发打包后的应用程序时需要它们**。当你把打包后的应用（含 `whisper-cli.exe` / `ffmpeg.exe` 等二进制）分发给他人时，须一并遵守各自许可证——这通常只需要在发布包里附带本说明与各组件的完整许可证文本，并不会让你的自有 MIT 源码变成 copyleft（它们是独立进程 / 构建工具 / 单纯聚合）。

| 组件 | 许可证 | 上游 / 作者 | 用途 | 分发时应履行的义务 |
|---|---|---|---|---|
| **whisper.cpp**（whisper-cli） | MIT | Georgi Gerganov · https://github.com/ggerganov/whisper.cpp | 本地语音识别（子进程调用） | 附带其 MIT 许可证与版权声明（若随包分发 `whisper-cli.exe`） |
| **OpenBLAS**（libopenblas.dll） | BSD-3-Clause | OpenMathLib · https://github.com/OpenMathLib/OpenMathLib | whisper.cpp 的 BLAS（CPU 加速）后端 | 附带其 BSD-3-Clause 许可证（若随包分发 `libopenblas.dll` / `ggml-blas.dll`） |
| **OpenAI Whisper（模型权重 ggml-*.bin）** | MIT | OpenAI · https://github.com/openai/whisper | 语音识别模型，由 whisper.cpp 加载 | 若随包分发模型文件，附带 OpenAI Whisper 的 MIT 许可证 |
| **FFmpeg**（ffmpeg / ffprobe） | **GPL 或 LGPL**（取决于你下载的具体构建） | FFmpeg 团队 · https://ffmpeg.org | 抽取 WAV、切割 / 重封装视频 | 提供 FFmpeg 许可证及对应源码获取方式；请确认你所用构建为 GPL 还是 LGPL，并按其要求提供源码或替换手段 |
| **PyInstaller**（bootloader） | **GPL + 特殊例外**（允许分发非自由程序，前提是不修改 bootloader） | PyInstaller 团队 · https://pyinstaller.org | 将源码冻结为单文件 exe（仅构建期） | 附带 PyInstaller 许可证声明；不要修改其 bootloader |

## 重要说明

- 本项目**未复制**上述任何组件的源码；whisper.cpp / ffmpeg 仅作为外部可执行文件经子进程调用，PyInstaller 仅用于打包。
- 仅把**源码仓库**以 MIT 发布是干净的；但若**分发包含上述二进制的成品应用**，请在本发布包内放入本文件及各组件的完整许可证文本（FFmpeg/GPL 还需提供对应源码或源码获取链接）。
- 若你另行引入了其它第三方库（如未来改用 `pip` 依赖），请在此处补充对应许可证与版权声明。
