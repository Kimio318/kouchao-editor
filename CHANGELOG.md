# 更新日志（CHANGELOG）

## v1.8（当前）

- **语音识别模型分级与自动选择**
  - 中文 / 英文默认走 `base` 模型；粤语走 `large-v3-turbo`（含 `<|yue|>` token）。
  - 直接读 ggml 文件头 `n_vocab` 判断粤语能力，无需加载整个模型。
  - 档位评分 + 同档位优先量化小模型 + 优先纯 ASCII 路径的自动选模策略。
- **复读循环根治**
  - 解码层加 `-mc 0`（断开跨段上下文）+ `-bs 1`（贪心解码提速）。
  - 事后三层去复读：字符级 / 短句级（`window=2` 覆盖交替复读）/ 词级（字幕）。
  - 复读率计算用于识别崩坏告警。
- **粤语转写校正表 `_YUE_FIXES`**
  - 针对粤语口播高频错写（他們→佢哋、資迅→资讯、基義→AI 等）的规则化校正，仅 yue 输出生效。
  - 持续按真实样本扩充（繁简两体皆收）。
- **编码稳定（Windows GBK 936）**
  - `_argv_non_ascii_safe()` 仅在系统为 UTF-8（ACP 65001）时经 argv 传中文提示词；GBK 下安全跳过，由校正表兜底。
  - `_ascii_model_path()` 把含中文 / 空格的模型路径复制到 ASCII 临时路径，规避 whisper 崩溃。
- **BLAS 加速与 CPU / GPU 调度**
  - 内置 whisper 为 OpenBLAS（BLAS）CPU 构建，纯 CPU 大幅加速。
  - `gpu_backend_available()` 探测 GPU 后端 DLL；前端 UI 支持「CPU / CPU+GPU」切换，无 GPU 后端时回退 CPU。
- **二进制定位机制**
  - 冻结 exe 从自身 `BASE/whisper/` 取 whisper 与模型，整包可移植、可免重打包替换 whisper。
- **仓库与文档**
  - 源码入库（重型依赖 / 模型 / 媒体经 `.gitignore` 排除），补充 README、LICENSE、.gitattributes。
