# 口播剪辑器（KouchaoEditor）

本地优先的口播视频自动剪辑器：基于静音检测切除停顿 / 语气词，并用 **whisper.cpp** 做本地语音识别，自动生成字幕（SRT / VTT）与文案。全流程在本地完成，**不上传任何服务器**。

> 当前版本：**v1.8**（见 `KouchaoEditor/` 子目录）。根目录下的 `cut_server.py`、`serve.py`、`kouchao_native.html`、`kouchao-cutter.html` 及 `_e2e_test*.py` 为早期原型 / 端到端测试脚本，仅供回溯参考，生产入口为 `KouchaoEditor/`。

## 功能

- 静音检测 + 停顿切除 / 压缩，可自动切分成多段话题视频
- 两级语气词切除（安全级 / 语境级），切点向静音边界吸附，避免切掉字头字尾
- whisper 本地语音识别：**中文（zh）走 `base` 模型，粤语（yue）走 `large-v3-turbo`**（含粤语 token）
- 输出 SRT / VTT 字幕 + 文案，统一繁→简，带字符级 / 短句级 / 词级三层去复读（无复读伪影）
- 后端为纯 Python 标准库 HTTP 服务（零第三方依赖），前端为 HTML，PyInstaller 打包为单文件 exe

## 核心架构

```
┌────────────┐    HTTP/JSON    ┌──────────────────────┐    subprocess    ┌────────────────┐
│  前端 HTML  │ ─────────────▶ │  kouchao_server.py    │ ───────────────▶ │ whisper-cli.exe │
│ (webview)  │ ◀───────────── │  (纯 stdlib 服务)     │                  │  (本地 ASR)     │
└────────────┘   渲染结果      └──────────────────────┘ ◀─────────────── └────────────────┘
                                          │ 调用
                                          ▼
                                   ffmpeg / ffprobe（抽取 WAV、切割视频）
```

- **后端** `KouchaoEditor/src/kouchao_server.py`：仅用标准库（http.server / subprocess / struct / ctypes），可被 PyInstaller 冻结进单文件 exe。
- **前端** `KouchaoEditor/index.html`：GUI web 界面，经本地 HTTP（`http://127.0.0.1:8021`）与后端交互。
- **语音识别** 调用本机 `whisper-cli.exe`（whisper.cpp），经 `-ojf` 产出含逐词时间戳的 JSON 后解析。
- **ffmpeg** 负责抽取 WAV、按切点切割并重封装视频。

## 目录结构

```
KouchaoEditor/
  src/kouchao_server.py   # 后端 HTTP 服务（纯 stdlib，被冻结进 exe）
  index.html               # 前端界面（GUI webview）
  KouchaoEditor.spec       # PyInstaller 打包配置
  start.bat                # 启动脚本
  README.txt               # 原使用说明
  whisper/                 # whisper.cpp（见下方「依赖」，gitignore）
    whisper-cli.exe + dlls（含 ggml-blas.dll / libopenblas.dll 等）
    models/                # ggml-*.bin（见下方「依赖」，gitignore）
  ffmpeg/                  # ffmpeg / ffprobe（gitignore）
cut_server.py、serve.py、kouchao_native.html、kouchao-cutter.html   # 早期原型
test_deloop.py、test_integration.py、test_subtitles.py             # 单元测试
_e2e_test.py、_e2e_test2.py                                         # 端到端测试脚本
```

## 技术要点

### 1. 模型选择与语言处理

whisper 的「粤语」是一个独立语言 token `<|yue|>`，**只存在于 large-v3 系列词表**（n_vocab = 51866）；99 语言词表（tiny/base/small/medium/large-v1/v2，n_vocab = 51865）没有 yue。因此：

- **中文 / 英文**：默认走 `ggml-base.bin`（base 档，速度快、够用）。
- **粤语**：必须使用 `ggml-large-v3-turbo-q8_0.bin`（large-v3-turbo 档，含 yue token），否则只能按普通话硬解，出现「而家→延載」「睇→採讀」这类音近字乱码。

模型自动选择逻辑（`kouchao_server.py`）：

- `_read_ggml_meta()` 直接读 ggml 文件头前 28 字节取出 `n_vocab`，**不加载整个模型**，据此判断是否具有粤语能力。
- 档位评分 `_MODEL_TIERS`：`large-v3-turbo (100) > large-v3 (98) > large-v2 (90) > large (88) > medium (70) > small (50) > base (30) > tiny (10)`；`.en` 单语模型对中文/粤语无用，压到最低分。
- `_best_model()`：同档位下优先体积更小的量化模型（`q8_0`/`q5_0`），CPU 上读权重更快、更不易超时；再优先纯 ASCII 路径（免去复制中文/空格路径模型的开销）。
- `select_asr_model(lang)`：选 `yue` 且有粤语模型时用大模型；否则用通用默认（中文/英文走较快模型）。可用环境变量 `WHISPER_BIN` / `WHISPER_MODEL` / `WHISPER_YUE_MODEL` 强制指定。

### 2. 复读循环清理

whisper 在长音频上会「自我复读」：默认 `--max-context -1`（无限历史文本条件化）一旦某段解码出错，错误文本被当作上下文喂给下一段，于是同一句被重复十几次（典型：`長載你所採讀的,長載你所採讀的,…`）。本程序从两层根治：

- **解码层**：调用 whisper 时固定加 `-mc 0`（断开跨段上下文），并用 `-bs 1`（贪心解码，比默认 beam=5 快约 5 倍、对识别率影响很小）。
- **事后清理**（三层）：
  - 字符级 `_collapse_char_loop()`：连续重复 ≥3 次的片段坍缩成一次；
  - 短句级 `_dedup_clauses()`：按标点切句，丢弃与前 2 句中任一句完全相同的句子（同时覆盖 `A,A,A` 与 `A,B,A,B` 交替复读）；
  - 词级 `_dedup_words()`：用于字幕，保留首次出现的时间戳、丢弃后续重复，避免字幕连刷。
  - `_clean_transcript()` 还会算出**复读率**（0~1），用于识别已崩的质量告警。

### 3. 粤语转写校正表 `_YUE_FIXES`

即便用 `-l yue` 解码，whisper 仍常把粤语读音「写成普通话书面语」（他們/還有沒有/我們）或「音对字错」（牽花板/人知/資迅/基義）。这些是落字错误而非口音问题，用一份针对粤语口播的高频错写→正写规则在输出后修正，成本低、风险小、可叠加在提示词之上。

- 定义见 `_YUE_FIXES`（`kouchao_server.py`），为 `(错写, 正写)` 元组列表，仅 `lang == 'yue'` 时由 `_correct_yue()` 应用，避免误伤普通话输出。
- 规则按列表顺序（长→短）匹配，长短语优先于其中的短词；末尾统一 `_to_simplified()` 繁→简兜底。
- **如何扩充**：遇到新的粤语错写，直接往 `_YUE_FIXES` 追加 `("错写", "正写")` 即可（繁简两体都可收，提示词改简体后以简体为主）。真实样本越多，校正越准。

> 配套：`_PROMPT_YUE` / `_PROMPT_ZH` 为初始提示词（initial prompt），在每段前置以引导书面粤语 / 普通话；`_T2S_MAP` 为内置繁→简单字映射（零依赖、可打包，跳过後/乾/復等歧义字，不改动粤语特有字嘅喺咁啲佢哋等）。

### 4. Beta UTF-8 与 GBK（936）编码处理

中文 Windows 的 ACP（活动代码页）默认 **936（GBK）**。whisper-cli 入口是 C 的 `main(int, char**)`：argv 由 CRT 按系统 ACP 从 Unicode 命令行转换，而 whisper.cpp 内部按 UTF-8 解释 argv。于是：

- 当 ACP ≠ 65001（UTF-8）时，中文提示词以 GBK 字节到达、被误读成 UTF-8，变成无意义字节回退 token——比不传提示词更糟。
- 因此 `_argv_non_ascii_safe()` 调用 `GetACP()` 判断：仅当系统为 UTF-8（65001）才把中文提示词经命令行传给 whisper-cli；**GBK 机器上自动跳过提示词**，由 `_YUE_FIXES` 校正表兜底大部分偏差。
- 模型路径含中文 / 空格也会导致 whisper 崩溃，故有 `_ascii_model_path()` 在启动时把模型复制到 ASCII 临时路径再调用。

**启用提示词（可选）**：Windows「设置 → 时间和语言 → 语言和区域 → 管理语言设置 → 更改系统区域设置」勾选 **「Beta: 使用 Unicode UTF-8 提供全球语言支持」** 并重启，ACP 变为 65001，届时粤语 / 中文提示词自动生效。

### 5. BLAS 加速与 CPU / GPU 调度

- **CPU 加速（BLAS）**：语音识别的算力瓶颈在 ggml 的 CPU 矩阵乘。把 `ggml-blas.dll` + `libopenblas.dll` 与 `whisper-cli.exe` 放在同一目录即为 **BLAS 构建**（基于 OpenBLAS 后端），相比无 BLAS 的原生构建在纯 CPU 机器上大幅加速——large-v3-turbo 在 CPU-only 设备的可用性主要来源于此。克隆仓库后需自行补齐这两个 DLL（见「依赖」）。
- **GPU 调度**：`gpu_backend_available()` 探测 whisper 目录下是否存在 GPU 后端 DLL（`ggml-cuda.dll` / `ggml-vulkan.dll` / `ggml-metal.dll` / `ggml-clblast.dll` / `ggml-sycl.dll`）。前端 UI 提供「CPU」/「CPU+GPU」切换并透传 `gpu` 参数：选 CPU 时显式加 `-ng`（`--no-gpu`）；选 CPU+GPU 时若 whisper 构建带 GPU 后端则走显卡，否则回退 CPU。
- **当前默认构建**：内置打包的 whisper-cli 为**纯 CPU（BLAS）构建**，无 GPU 后端 DLL，故「CPU+GPU」模式会回退到 CPU。若要启用显卡加速，换用带对应后端 DLL 的 whisper 构建即可，无需改代码。

### 6. 二进制定位机制（`find_whisper`）

冻结后的 exe 从**自身所在目录的 `BASE/whisper/`** 取 `whisper-cli.exe` 与 `models/` 模型，因此**整包复制到任意 Windows 电脑双击即用，且无需重打包即可替换 whisper 版本**；开发态则回退到 `E:\口播测试\whisper\`、环境变量、`shutil.which` 等路径。

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

## 常见问题 / 故障排查

| 现象 | 原因与处理 |
|---|---|
| 提示「未找到 whisper 语音识别」 | `KouchaoEditor/whisper/whisper-cli.exe` 或 `models/ggml-*.bin` 未就位；刷新页面（服务会重新探测）或检查路径。 |
| 粤语被听成普通话同音字（延載/採讀） | 当前模型为 99 语言词表（无 yue）。换用 `ggml-large-v3-turbo.bin` / `ggml-large-v3.bin`。 |
| 粤语转写仍有错别字（他們/資迅） | 属落字错误，由 `_YUE_FIXES` 校正；遇到新错写按上文追加规则。 |
| 字幕出现大段重复 | 解码层 `-mc 0` + 三层去复读已处理；若仍严重，复读率告警会提示识别已崩，建议换更短片段或更大模型。 |
| 中文提示词似乎没生效 | 系统 ACP 为 936（GBK），提示词被安全跳过；开启系统「Beta UTF-8」并重启即可启用（GBK 下由校正表兜底）。 |
| 「CPU+GPU」仍很慢 | 内置 whisper 为纯 CPU 构建，无 GPU 后端 DLL；换带 GPU 后端的构建才会真正用显卡。 |

## 版本与状态

- 版本：**v1.8**（部署于 `KouchaoEditor/`）。
- 后端零第三方依赖；语音识别默认仅 CPU（BLAS），支持切换到 GPU 构建。
- 已知限制：GBK 系统下粤语 / 中文提示词经 argv 不安全，已用校正表兜底；纯 CPU 跑 large 模型 + 长视频耗时较长（超时兜底 2 小时），提速靠量化模型或 GPU 后端。

## 许可与第三方组件

- **本仓库自有源码**（Python 标准库实现 + 前端 HTML）以 **MIT 许可证** 发布，版权归 **Kimio318** 所有，详见 [LICENSE](./LICENSE)。每份源码文件顶部均含 `SPDX-License-Identifier: MIT` 声明。
- **whisper.cpp / OpenAI Whisper 模型权重 / OpenBLAS / FFmpeg / PyInstaller** 均为**独立第三方组件**：仅运行时经子进程调用或构建期使用，其源码**不在本仓库内**（经 `.gitignore` 排除），与本仓库自有代码无派生关系。
- 各第三方组件的许可证与分发时的义务，统一见 [THIRD-PARTY-NOTICES](./THIRD-PARTY-NOTICES.md)。
- **重要**：将打包后的应用（含 `whisper-cli.exe` / `ffmpeg.exe` 等二进制）分发给他人时，须按 THIRD-PARTY-NOTICES 在发布包内附带相应许可证文本（打包配置已自动把 `LICENSE` 与 `THIRD-PARTY-NOTICES.md` 打进发布包）。
