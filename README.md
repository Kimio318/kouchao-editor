# 口播剪辑器（KouchaoEditor）

本地优先的口播视频自动剪辑器：基于静音检测切除停顿 / 语气词，并用 whisper.cpp 做本地语音识别，自动生成字幕（SRT / VTT）与文案。全流程在本地完成，**不上传任何服务器**。

## 功能

- 静音检测 + 停顿切除 / 压缩，可自动切分成多段话题视频
- 两级语气词切除（安全级 / 语境级），切点向静音边界吸附，避免切掉字头字尾
- whisper 本地语音识别：中文（zh）走 `base` 模型，粤语（yue）走 `large-v3-turbo`（含粤语 token）
- 输出 SRT / VTT 字幕 + 文案，统一繁→简，带词级去复读（无复读伪影）
- 后端为纯 Python 标准库 HTTP 服务，前端为 HTML，PyInstaller 打包为单文件 exe

## 目录结构

```
KouchaoEditor/
  src/kouchao_server.py   # 后端 HTTP 服务（纯 stdlib，被冻结进 exe）
  index.html               # 前端界面（GUI webview）
  KouchaoEditor.spec      # PyInstaller 打包配置
  start.bat                # 启动脚本
  README.txt               # 原使用说明
  whisper/                 # whisper.cpp（见下方「依赖」，gitignore）
    whisper-cli.exe + dlls
    models/                # ggml-*.bin（见下方「依赖」，gitignore）
  ffmpeg/                  # ffmpeg / ffprobe（gitignore）
kouchao_native.html、kouchao-cutter.html、cut_server.py、serve.py   # 早期原型
test_deloop.py、test_integration.py、test_subtitles.py             # 单元测试
_e2e_test.py、_e2e_test2.py                                       # 端到端测试脚本
```

## 依赖（不入库，克隆后需本地补齐）

重型二进制按 `.gitignore` 排除，克隆仓库后需自行放置到对应路径：

1. **whisper.cpp（BLAS 构建）**：把 `whisper-cli.exe` 与全部 dll（含 `ggml-blas.dll`、`libopenblas.dll`、`ggml-cpu-*.dll`）放到 `KouchaoEditor/whisper/`。
2. **模型**：
   - 中文：`ggml-base.bin`（约 141MB）→ `KouchaoEditor/whisper/models/`
   - 粤语：`ggml-large-v3-turbo-q8_0.bin`（约 834MB）→ `KouchaoEditor/whisper/models/`
3. **ffmpeg**：把 `ffmpeg.exe` + `ffprobe.exe` 放到 `KouchaoEditor/ffmpeg/`。

> 以上二进制可从原开发环境直接复制，或自行下载对应构建放到上述路径即可，无需改动代码。

## 运行

- **成品**：双击 `KouchaoEditor.exe`（或 `start.bat`），界面自动打开，后端默认服务 `http://127.0.0.1:8021`。
- **开发**：直接 `python KouchaoEditor/src/kouchao_server.py`（需本机 `KouchaoEditor/ffmpeg/`、`KouchaoEditor/whisper/` 已就位）。

## 打包

```bat
pyinstaller KouchaoEditor/KouchaoEditor.spec
```

生成单文件 `KouchaoEditor.exe`；部署时 `whisper/`、`ffmpeg/`、`whisper/models/` 随程序目录一起分发。

## 备注

- 后端仅依赖 Python 标准库，无第三方包。
- 语音识别默认「仅 CPU」（`-ng`）；若使用带 GPU 后端的 whisper 构建，可在界面选择 GPU 模式。
- 本仓库仅含源码；模型 / ffmpeg 等体积较大，按 `.gitignore` 排除，请按上文在本地补齐后再运行。
