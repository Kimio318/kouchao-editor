#!/usr/bin/env python3
# Copyright (c) 2026 Kimio318
# SPDX-License-Identifier: MIT
# KouchaoEditor（口播剪辑器）—— 自有源码，采用 MIT 许可证（详见 LICENSE）。

# 口播自动剪辑器 —— 便携版后端（单文件，可被 PyInstaller 打成 exe）
# 仅用 Python 标准库；视频全程本地处理，不上传任何服务器。
# ffmpeg / whisper 均从本程序所在目录的相对子文件夹中查找，因此整包可复制到任意 Windows 电脑双击使用。
#
# 剪辑策略（原创实现，纯本地启发式）：
#   1) 两级语气词：安全级（无意义犹豫音）始终切；语境级（连接/顺序词）只在贴近停顿边界时切，避免误伤。
#   2) 停顿处理：默认“切除”；可选“压缩”——长静音压到固定短间隙，保留自然呼吸感。
#   3) 切点吸附：语气词切除区间向最近的静音边界吸附，避免切在发音中途、把句子粘在一起。
#   4) 自然切片：把超过阈值的长静音当作话题断点，自动切成多段独立视频。
import os, re, sys, json, shutil, glob, struct, tempfile, subprocess, html, threading, webbrowser, socket, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------- 定位「本程序所在目录」 ----------
# 冻结成 exe 后，__file__ 指向临时解压目录，必须用 sys.executable 拿到真实所在文件夹
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))

HERE = BASE
HTML_FILE = os.path.join(HERE, "index.html")


def _auto_port(start=8021, limit=20):
    for p in range(start, start + limit):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            continue
    return start


PORT = int(os.environ.get("CUT_PORT", _auto_port()))


# ---------- 定位 ffmpeg ----------
def find_ffmpeg():
    if os.environ.get("FFMPEG_BIN") and os.path.isfile(os.environ["FFMPEG_BIN"]):
        return os.environ["FFMPEG_BIN"]
    # 优先用「本程序所在目录」下的捆绑 ffmpeg（保证拷到别的电脑也能用）
    cands = [
        os.path.join(BASE, "ffmpeg", "ffmpeg.exe"),
        os.path.join(BASE, "ffmpeg", "bin", "ffmpeg.exe"),
        os.path.join(BASE, "bin", "ffmpeg.exe"),
        r"E:\口播测试\ffmpeg.exe",
        r"E:\口播测试\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\ffmpeg\ffmpeg.exe",
        os.path.join(HERE, "ffmpeg.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    # PATH 中若恰好有 ffmpeg 也可用（放在最后，避免覆盖捆绑版）
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        for root, _, files in os.walk(BASE):
            for f in files:
                if f.lower() == "ffmpeg.exe":
                    return os.path.join(root, f)
    except Exception:
        pass
    return None


FFMPEG = find_ffmpeg()
FFPROBE = os.path.join(os.path.dirname(FFMPEG), "ffprobe.exe") if FFMPEG else None


# ---------- 定位 whisper.cpp ----------
def find_whisper():
    if os.environ.get("WHISPER_BIN") and os.path.isfile(os.environ["WHISPER_BIN"]):
        return os.environ["WHISPER_BIN"]
    cands = [
        os.path.join(BASE, "whisper", "whisper-cli.exe"),
        os.path.join(BASE, "whisper", "bin", "whisper-cli.exe"),
        os.path.join(BASE, "bin", "whisper-cli.exe"),
        r"E:\口播测试\whisper\whisper-cli.exe",
        r"E:\口播测试\whisper.exe",
        os.path.join(HERE, "whisper-cli.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    p = shutil.which("whisper-cli") or shutil.which("whisper")
    if p:
        return p
    try:
        for root, _, files in os.walk(BASE):
            for f in files:
                if f.lower() in ("whisper-cli.exe", "whisper.exe"):
                    return os.path.join(root, f)
    except Exception:
        pass
    return None


# ---------- 模型档位与能力探测 ----------
# 说明：Whisper 的「粤语」是一个独立语言 token <|yue|>，它只存在于 large-v3 系列的词表里。
#   - 99 语言词表（tiny/base/small/medium/large-v1/v2）：n_vocab = 51865，**没有** yue
#   - 100 语言词表（large-v3 / large-v3-turbo）：n_vocab = 51866，多出来的就是 yue
# 所以想真正识别粤语，必须用 large-v3 系模型；否则只能按普通话(zh)硬解，
# 结果就是「而家」被听成「延載」、「睇」被写成「採讀」这类音近字乱码。
_VOCAB_MULTILINGUAL = 51865   # 99 种语言
_VOCAB_WITH_YUE = 51866       # 100 种语言（含粤语 yue）

# 档位分数：越高越准。turbo 排在 large-v3 之前，因为纯 CPU 机器上 turbo 快数倍而精度只差一点，
# 是本程序（无 GPU 依赖）最实用的选择。
_MODEL_TIERS = [
    ("large-v3-turbo", 100, "large-v3-turbo"),
    ("large-v3",        98, "large-v3"),
    ("large-v2",        90, "large-v2"),
    ("large",           88, "large"),
    ("medium",          70, "medium"),
    ("small",           50, "small"),
    ("base",            30, "base"),
    ("tiny",            10, "tiny"),
]


def _model_tier(path):
    """按文件名判断模型档位，返回 (分数, 档位名)。.en 单语模型对中文/粤语无用，直接压到最低。"""
    n = os.path.basename(path or "").lower()
    if ".en." in n or n.endswith(".en.bin") or "-en.bin" in n:
        return (1, "english-only")
    for key, score, label in _MODEL_TIERS:
        if key in n:
            return (score, label)
    return (5, "unknown")


def _read_ggml_meta(path):
    """直接读 ggml 模型文件头，取出 n_vocab 等超参，用来判断模型能力。

    ggml whisper 模型头部布局（小端）：
        uint32 magic == 0x67676d6c ('ggml')
        int32  n_vocab, n_audio_ctx, n_audio_state, n_audio_head, n_audio_layer, n_text_ctx, ...
    只读前 28 字节即可，不用把整个模型载进内存。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(28)
        if len(head) < 28:
            return {}
        magic, n_vocab, n_audio_ctx, n_audio_state, n_audio_head, n_audio_layer, n_text_ctx = \
            struct.unpack("<Iiiiiii", head)
        if magic != 0x67676D6C:
            return {}
        return {
            "n_vocab": n_vocab,
            "n_audio_ctx": n_audio_ctx,
            "n_text_ctx": n_text_ctx,
            "multilingual": n_vocab >= _VOCAB_MULTILINGUAL,
            "hasYue": n_vocab >= _VOCAB_WITH_YUE,
        }
    except Exception:
        return {}


def model_info(path=None):
    """汇总当前模型的档位/能力，供 /api/asr-status 与错误提示使用。"""
    p = path or WHISPER_MODEL
    if not p or not os.path.isfile(p):
        return {"ok": False}
    score, tier = _model_tier(p)
    meta = _read_ggml_meta(p)
    try:
        size_mb = round(os.path.getsize(p) / 1048576)
    except OSError:
        size_mb = None
    # 粤语能力：优先信任实际读到的词表大小，读不到再退回文件名判断
    has_yue = meta.get("hasYue")
    if has_yue is None:
        has_yue = tier in ("large-v3", "large-v3-turbo")
    return {
        "ok": True,
        "path": p,
        "name": os.path.basename(p),
        "tier": tier,
        "tierScore": score,
        "sizeMB": size_mb,
        "nVocab": meta.get("n_vocab"),
        "multilingual": meta.get("multilingual", True),
        "hasYue": bool(has_yue),
        # 对粤语/中文口播来说，small 及以下基本不够用
        "weakForCJK": score <= 50,
    }


def _candidate_models():
    """收集所有候选 ggml-*.bin（去重、存在性校验）。"""
    pats = [
        os.path.join(BASE, "whisper", "models", "ggml-*.bin"),
        os.path.join(BASE, "whisper", "ggml-*.bin"),
        os.path.join(BASE, "models", "ggml-*.bin"),
        r"E:\口播测试\whisper\models\ggml-*.bin",
        r"E:\口播测试\whisper\*.bin",
        r"E:\口播测试\ggml-*.bin",
        os.path.join(HERE, "models", "ggml-*.bin"),
    ]
    cands = []
    for pat in pats:
        cands.extend(glob.glob(pat))
    # 递归兜底：只在 BASE 下找，避免扫到整个磁盘
    if not cands:
        try:
            for root, _, files in os.walk(BASE):
                for f in files:
                    if f.lower().startswith("ggml-") and f.lower().endswith(".bin"):
                        cands.append(os.path.join(root, f))
        except Exception:
            pass
    seen, out = set(), []
    for c in cands:
        c = os.path.normpath(c)
        if os.path.isfile(c) and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _best_model(pool):
    """在候选池里挑档位最高、且同档位下体积最小（量化版）的模型；池为空返回 None。
    选择策略：
      - 档位高优先（large-v3-turbo > large-v3 > … > base），保证识别精度上限；
      - 同档位时体积小的优先：CPU 上量化模型（q8_0 / q5_0 等）读权重更快、更不易超时，
        未量化 f16 原版反而最慢，所以量化小模型优于未量化大模型；
      - 同体积再优先纯 ASCII 路径（免去每次启动把中文/空格路径模型复制到临时目录的开销）。
    """
    if not pool:
        return None
    def _k(p):
        return (_model_tier(p)[0], -_safe_size(p), 1 if all(ord(c) <= 127 for c in p) else 0)
    return sorted(pool, key=_k, reverse=True)[0]


def _safe_size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def find_whisper_model():
    """通用默认模型：优先挑「非粤语」模型（让中文/英文走较快的模型），
    没有非粤语模型时退回所有模型里档位最高的。"""
    if os.environ.get("WHISPER_MODEL") and os.path.isfile(os.environ["WHISPER_MODEL"]):
        return os.environ["WHISPER_MODEL"]
    allm = _candidate_models()
    non_yue = [m for m in allm if not model_info(m).get("hasYue")]
    return _best_model(non_yue) or _best_model(allm)


def find_yue_model():
    """粤语专用模型：挑含粤语 token（n_vocab>=51866）且档位最高的；没有则返回 None。"""
    if os.environ.get("WHISPER_YUE_MODEL") and os.path.isfile(os.environ["WHISPER_YUE_MODEL"]):
        return os.environ["WHISPER_YUE_MODEL"]
    allm = _candidate_models()
    yue = [m for m in allm if model_info(m).get("hasYue")]
    return _best_model(yue)


WHISPER = find_whisper()
WHISPER_MODEL = find_whisper_model()
WHISPER_YUE_MODEL = find_yue_model()


def select_asr_model(lang):
    """按语言选模型：粤语且有粤语模型 → 用粤语模型；否则用通用默认模型。"""
    if str(lang or "").strip() == "yue" and WHISPER_YUE_MODEL:
        return WHISPER_YUE_MODEL
    return WHISPER_MODEL


# 模型路径缓存：键 = (原路径, mtime, 大小)，值 = ASCII 临时副本路径
_MODEL_CACHE = {}


def _ascii_model_path(model=WHISPER_MODEL):
    """把模型复制到一个纯 ASCII 的临时路径，再交给 whisper-cli。

    原因：whisper.cpp 的 whisper-cli.exe 在 Windows 上用 CRT 的 fopen 打开 -m 指定的模型文件。
    当该路径含非 ASCII 字符（中文/日文等）且系统活动代码页不是 UTF-8 时，fopen 会按错误代码页
    解码路径 → 文件要么找不到、要么读到的字节错乱 → 进程直接 fast-fail 崩溃
    （退出码 0xC0000409 / 3221226505），且不写任何输出。
    把模型复制到 ASCII 临时目录后，whisper 看到的 -m 就是纯 ASCII，彻底规避该问题。
    按 (路径,mtime,大小) 缓存，模型没换就不重复复制这 140MB 大文件；原位已是纯 ASCII 路径则零开销直接返回。
    """
    src = model
    if not src or not os.path.isfile(src):
        return src
    if all(ord(ch) <= 127 for ch in src):
        return src  # 已经是纯 ASCII（如 C 盘英文路径），无需复制
    try:
        key = (src, os.path.getmtime(src), os.path.getsize(src))
    except OSError:
        return src
    cached = _MODEL_CACHE.get(key)
    if cached and os.path.isfile(cached):
        return cached
    d = os.path.join(tempfile.gettempdir(), "kouchao_whisper_model")
    try:
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, os.path.basename(src))
        shutil.copyfile(src, dst)
    except OSError:
        return src
    _MODEL_CACHE[key] = dst
    return dst


def refresh_whisper():
    """每次请求重新探测 whisper 位置（放好文件后无需重启，刷新页面即可）"""
    global WHISPER, WHISPER_MODEL, WHISPER_YUE_MODEL
    WHISPER = find_whisper()
    WHISPER_MODEL = find_whisper_model()
    WHISPER_YUE_MODEL = find_yue_model()


# ---------- ffmpeg 辅助 ----------
def duration_of(path):
    if not FFPROBE:
        return 0.0
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        return float(out.stdout.strip() or 0)
    except Exception:
        return 0.0


def has_audio(path):
    if not FFPROBE:
        return True
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error",
             "-select_streams", "a", "-show_entries", "stream=index",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        return bool(out.stdout.strip())
    except Exception:
        return True


def run_silencedetect(path, noise, d):
    """返回静音区间列表 [[start,end],...]（秒），只报告持续 >= d 的静音"""
    cmd = [FFMPEG, "-i", path,
           "-af", f"silencedetect=noise={noise}dB:d={d}",
           "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    silences = []
    cur = None
    for line in (proc.stderr or "").splitlines():
        m = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m:
            cur = float(m.group(1))
        m2 = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m2:
            end = float(m2.group(1))
            if cur is not None:
                silences.append([cur, end])
            cur = None
    return silences


# ---------- 区间计算 ----------
def parse_set(s):
    return set(w.strip() for w in str(s or "").replace("，", ",").split(",") if w.strip())


def _norm(s):
    return re.sub(r"[\s，。、！？；：,.!?;:\"'`]+", "", s or "")


def snap_interval(ws, we, silences, tol, tier):
    """把语气词切除区间向最近的静音边界吸附。
    tier=1（安全级）：附近有静音就吸附到边界，否则小幅扩边避免切掉字尾。
    tier=2（语境级）：仅当贴着停顿边界才切，否则保留（避免误伤连接/顺序词）。"""
    before = None
    after = None
    for (ss, se) in silences:
        # 词前的停顿：其末尾必须贴近词头（只允许极小幅重叠）
        if se >= ws - tol and se <= ws + 0.06:
            before = se
        # 词后的停顿：其开头必须贴近词尾
        if ss >= we - 0.06 and ss <= we + tol:
            after = ss
    if tier == 1:
        s = before if before is not None else max(0.0, ws - 0.12)
        e = after if after is not None else we + 0.12
        return [s, e]
    # tier 2：必须贴近停顿边界才切
    if before is None and after is None:
        return None
    return [max(0.0, ws - 0.03), we + 0.03]


# ---------- 语音识别（whisper.cpp）----------
def extract_wav(path):
    wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    cmd = [FFMPEG, "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
           "-f", "wav", wav]
    subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    return wav


def hms_to_sec(t):
    t = t.strip()
    if ":" in t:
        parts = t.split(":")
        parts = [float(p) for p in parts]
        while len(parts) < 3:
            parts.insert(0, 0.0)
        h, m, s = parts
        return h * 3600 + m * 60 + s
    return float(t)


_TS_RE = re.compile(
    r"\[\s*(\d{1,2}:\d{2}:\d{2}\.\d{1,3}|\d+\.\d+)\s*-->\s*(\d{1,2}:\d{2}:\d{2}\.\d{1,3}|\d+\.\d+)\s*\]\s*(.*)")


def parse_timestamped_lines(text):
    out = []
    for line in text.splitlines():
        m = _TS_RE.search(line)
        if m:
            try:
                s = hms_to_sec(m.group(1))
                e = hms_to_sec(m.group(2))
                out.append((m.group(3).strip(), s, e))
            except Exception:
                pass
    return out


# ---------- 复读循环清理 ----------
# whisper 在长音频上会「自我复读」：一旦某一段解码出错，它会把错误文本当作上下文继续生成，
# 于是同一句被重复十几次，且越往后越离谱（典型现象：「長載你所採讀的,長載你所採讀的,……」）。
# 根因是 whisper-cli 默认 --max-context -1（无限历史文本条件化）——我们已在调用处改成 -mc 0。
# 这里再补一层事后清理：把已经生成的复读坍缩掉，并算出复读率用于质量告警。
_PUNCT = "，,。；;！!？?、…‥ \t\n\r"


def _collapse_char_loop(s, max_unit=16, min_reps=3):
    """字符级去复读：连续重复 >= min_reps 次的片段坍缩成一次。
    例：'長載你所採讀的,長載你所採讀的,長載你所採讀的' -> '長載你所採讀的,'
    """
    if not s:
        return s
    guard = 0
    while guard < 200:          # 防御性上限，避免任何意外导致死循环
        guard += 1
        hit = False
        for n in range(1, max_unit + 1):
            i = 0
            while i + n * min_reps <= len(s):
                unit = s[i:i + n]
                if not unit.strip(_PUNCT):      # 纯标点/空白不算复读单元
                    i += 1
                    continue
                reps = 1
                while s[i + reps * n: i + (reps + 1) * n] == unit:
                    reps += 1
                if reps >= min_reps:
                    s = s[:i + n] + s[i + reps * n:]
                    hit = True                  # 原地不前进，继续查同一位置的嵌套复读
                else:
                    i += 1
            if hit:
                break
        if not hit:
            break
    return s


def _clause_key(c):
    return c.strip(_PUNCT)


def _dedup_clauses(text, window=2):
    """短句级去复读：按标点切句，丢掉与前 window 句中任一句完全相同的句子。
    window=2 同时覆盖 'A,A,A'（连续复读）与 'A,B,A,B'（交替复读）两种形态。
    """
    if not text:
        return text
    parts = re.split(r"(?<=[，,。；;！!？?、\n])", text)
    kept, keys = [], []
    for p in parts:
        k = _clause_key(p)
        if not k:
            kept.append(p)
            continue
        if k in keys[-window:]:
            continue
        kept.append(p)
        keys.append(k)
    return "".join(kept)


def _clean_transcript(text):
    """清掉文案里的复读循环。返回 (清理后文本, 复读率 0~1)。复读率高说明识别已崩。"""
    if not text:
        return text, 0.0
    # 1) 行级：连续完全相同的行只留一行
    dl, prev = [], None
    for ln in text.splitlines():
        k = ln.strip()
        if k and k == prev:
            continue
        dl.append(ln)
        if k:
            prev = k
    # 2) 行内：短句级 + 字符级
    cleaned = "\n".join(_collapse_char_loop(_dedup_clauses(ln)) for ln in dl)

    def _clauses(t):
        return [x for x in (_clause_key(y) for y in re.split(r"[，,。；;！!？?、\n]", t)) if x]

    a, b = _clauses(text), _clauses(cleaned)
    ratio = 0.0 if not a else max(0.0, 1.0 - len(b) / len(a))
    return cleaned, round(ratio, 3)


def _dedup_words(words, max_unit=12, min_reps=3):
    """词级去复读：同一串 n 个词连续重复 >= min_reps 次时只保留第一次。
    保留首次出现的时间戳、丢弃后续重复 —— 这样字幕不会连刷十几条相同内容，
    语气词切点也不会被复读文本污染。返回 (清理后词表, 丢弃个数)。
    """
    n_total = len(words)
    if n_total < 2:
        return words, 0
    txt = [w.get("word", "") for w in words]
    out, i = [], 0
    while i < n_total:
        collapsed = False
        for n in range(1, max_unit + 1):
            if i + n * min_reps > n_total:
                break
            unit = txt[i:i + n]
            reps = 1
            while True:
                j = i + reps * n
                if j + n > n_total or txt[j:j + n] != unit:
                    break
                reps += 1
            if reps >= min_reps:
                out.extend(words[i:i + n])
                i += reps * n
                collapsed = True
                break
        if not collapsed:
            out.append(words[i])
            i += 1
    return out, n_total - len(out)


# 粤语/普通话的 initial prompt：用目标语言的典型用字给模型「打个底」，
# 引导它输出书面粤语，而不是把粤语读音硬凑成普通话词（「而家」→「延載」那类错误）。
_PROMPT_YUE = ("以下为粤语口语录音，请用简体中文书面粤语完整转写全部语音，"
               "不得省略、跳过或合并任何一句；保留粤语用字（嘅、喺、咁、啲、唔、嗰、乜、"
               "点样、而家、睇、讲、嘢、谂、攰），并严格按发音直写，"
               "切勿将粤语词改写为相近的普通话词（例如「佢哋」勿写成「他们」、「我哋」勿写成「我们」）。")
_PROMPT_ZH = "以下是普通话口播录音，请用简体中文转写。"

# ---------- 粤语转写校正表 ----------
# whisper 用 -l yue 解码粤语时，仍常把粤语读音「写成普通话书面语」（他們/還有沒有/我們）
# 或「音对字错」（牽花板/人知/資迅/基義）。这些大多不是口音问题，而是落字错误，
# 用一份针对粤语口播的高频错写->正写规则在输出后修正，成本低、风险小、可叠加在提示词之上。
# 仅在 lang=='yue' 时应用，避免误伤普通话输出。规则按长度降序匹配，长短语优先于其中的短词。
_YUE_FIXES = [
    ("稍用自己的事件", "善用自己嘅优势"),
    ("比我們差不多", "同我哋差唔多"),
    ("還有沒有", "有冇"),
    ("北京的NBA", "读紧MBA"),
    ("北京和AMP開", "併读MBA"),
    ("基義大陸", "AI工具"),
    ("限定生文", "资讯时代"),
    ("有多路", "有几叻"),
    ("人知邊界", "认知边界"),
    ("大拍错", "大错"),
    ("他們", "佢哋"),
    ("她們", "佢哋"),
    ("它們", "佢哋"),
    ("我們", "我哋"),
    ("你們", "你哋"),
    ("牽花板", "天花板"),
    ("人知", "认知"),
    ("定僧", "竞争"),
    ("资 迅", "资讯"),
    ("资迅", "资讯"),
    ("資迅", "资讯"),
    ("创与", "创业"),
    ("稍用", "善用"),
    ("基義", "AI"),
    ("印論", "舆论"),
    ("超量", "招数"),
    ("学书", "学嘢"),
    ("学生", "学识"),
    ("学悉", "学识"),
    # 本轮新增（来自实测偏差样本，粤语口播高频错写；繁简两体都收，提示词改简体后仍以简体为主）
    ("老闔", "老闆"),
    ("摸雨", "摸鱼"),
    ("一臉", "一体"),
    ("一脸", "一体"),
    ("人不为己牽之地", "人不为己天诛地灭"),
    ("擦手機", "搓手机"),
    ("擦手机", "搓手机"),
    ("抬工", "怠工"),
]

def _correct_yue(text):
    """针对粤语转写的常见错写做规则化校正（仅用于 yue 输出）。"""
    if not text:
        return text
    for wrong, right in _YUE_FIXES:
        if wrong in text:
            text = text.replace(wrong, right)
    return _to_simplified(text)


# 繁→简 兜底转换：提示词已要求简体，但 whisper 偶尔仍会吐个别繁体常用字。
# 用内置映射表（零依赖、可打包）统一转简体。仅收录「繁≠简」的明确单字对应，
# 跳过後/乾/復等歧义字，避免误改正确文本。Cantonese 特有字（嘅喺咁啲佢哋等）两体相同，不受影响。
_T2S_MAP = {
    "麽": "么", "這": "这", "來": "来", "國": "国", "動": "动", "沒": "没", "還": "还",
    "體": "体", "為": "为", "個": "个", "們": "们", "話": "话", "對": "对", "後": "后",
    "實": "实", "關": "关", "員": "员", "勞": "劳", "廠": "厂", "經": "经", "濟": "济",
    "題": "题", "顧": "顾", "職": "职", "務": "务", "場": "场", "電": "电", "腦": "脑",
    "線": "线", "結": "结", "構": "构", "謝": "谢", "網": "网", "絡": "络", "圖": "图",
    "書": "书", "館": "馆", "鐘": "钟", "錶": "表", "頭": "头", "賣": "卖", "買": "买",
    "貴": "贵", "錢": "钱", "費": "费", "貨": "货", "輸": "输", "運": "运", "機": "机",
    "飛": "飞", "馬": "马", "魚": "鱼", "鳥": "鸟", "雞": "鸡", "鴨": "鸭", "紅": "红",
    "綠": "绿", "藍": "蓝", "黃": "黄", "點": "点", "熱": "热", "愛": "爱", "親": "亲",
    "間": "间", "陽": "阳", "陰": "阴", "雲": "云", "風": "风", "門": "门", "問": "问",
    "聞": "闻", "閒": "闲", "顏": "颜", "類": "类", "麵": "面", "萬": "万", "歲": "岁",
    "歷": "历", "醫": "医", "藥": "药", "補": "补", "裝": "装", "視": "视", "覺": "觉",
    "見": "见", "觀": "观", "規": "规", "則": "则", "參": "参", "變": "变", "態": "态",
    "證": "证", "議": "议", "論": "论", "設": "设", "計": "计", "試": "试", "驗": "验",
    "調": "调", "環": "环", "護": "护", "導": "导", "師": "师", "團": "团", "積": "积",
    "極": "极", "權": "权", "現": "现", "眾": "众", "總": "总", "統": "统", "組": "组",
    "織": "织", "終": "终", "給": "给", "細": "细", "號": "号", "樣": "样", "認": "认",
    "識": "识", "兩": "两", "東": "东", "車": "车", "預": "预", "誰": "谁", "應": "应",
    "會": "会", "時": "时", "與": "与", "語": "语", "錯": "错", "邊": "边", "長": "长",
    "據": "据", "讓": "让", "說": "说", "發": "发", "產": "产", "進": "进", "當": "当",
    "處": "处", "過": "过", "開": "开", "單": "单", "張": "张", "義": "义", "戰": "战",
    "勝": "胜", "讀": "读", "寫": "写", "詞": "词", "頁": "页", "項": "项", "輕": "轻",
    "軍": "军", "農": "农", "華": "华", "畫": "画", "備": "备", "價": "价", "帶": "带",
    "婦": "妇", "聯": "联", "連": "连", "週": "周", "園": "园", "圓": "圆", "縣": "县",
    "嚴": "严", "豐": "丰", "灣": "湾", "塊": "块", "壞": "坏", "幫": "帮", "黨": "党",
    "專": "专", "業": "业", "報": "报", "爾": "尔", "畝": "亩", "壇": "坛", "廣": "广",
    "莊": "庄", "廳": "厅", "審": "审", "憲": "宪", "寵": "宠", "屬": "属", "層": "层",
    "島": "岛", "嶺": "岭", "帥": "帅", "並": "并", "巖": "岩", "巔": "巅",
    "復": "复", "複": "复", "髮": "发", "裡": "里", "臺": "台", "幹": "干", "製": "制",
    "徵": "征", "鬆": "松", "穀": "谷", "鹹": "咸", "佔": "占", "捨": "舍", "隻": "只",
    "瞭": "了", "範": "范", "鬱": "郁", "幾": "几", "術": "术", "傭": "佣", "塗": "涂",
    "捲": "卷", "據": "据", "摺": "折", "僕": "仆", "釁": "衅", "髒": "脏", "餘": "余",
    "採": "采", "籲": "吁", "麯": "曲", "纖": "纤", "簾": "帘", "嚮": "向", "剋": "克",
    "闢": "辟", "鬥": "斗", "繫": "系", "係": "系", "傢": "家", "傚": "效", "僱": "雇",
    "償": "偿", "優": "优", "勢": "势", "勵": "励", "勳": "勋", "匯": "汇", "區": "区",
    "劃": "划", "劇": "剧", "勸": "劝", "廚": "厨", "廢": "废", "弒": "弑", "強": "强",
    "彥": "彦", "彫": "雕", "徑": "径", "徹": "彻", "憂": "忧", "憐": "怜", "憑": "凭",
    "憤": "愤", "憫": "悯", "懇": "恳", "懲": "惩", "懶": "懒", "戶": "户", "揚": "扬",
    "搖": "摇", "攝": "摄", "敵": "敌", "數": "数", "無": "无", "曉": "晓", "殺": "杀",
    "氣": "气", "決": "决", "異": "异", "監": "监", "確": "确", "礙": "碍", "離": "离",
    "種": "种", "稱": "称", "競": "竞", "筆": "笔", "築": "筑", "糧": "粮", "約": "约",
    "純": "纯", "紙": "纸", "級": "级", "續": "续",
    # 补充高频常用字（含上文样本实测残留）
    "麼": "么", "緊": "紧", "責": "责", "創": "创", "簽": "签", "爛": "烂",
    "達": "达", "講": "讲", "課": "课", "轉": "转", "辦": "办", "遲": "迟",
    "選": "选", "遠": "远", "違": "违", "遊": "游", "亂": "乱", "悶": "闷",
    "閱": "阅", "隨": "随", "險": "险", "難": "难", "靜": "静", "頂": "顶",
    "順": "顺", "領": "领", "飲": "饮", "飾": "饰", "飢": "饥", "韋": "韦",
    "響": "响", "騎": "骑", "駛": "驶", "餓": "饿", "飯": "饭",
}
_T2S_TABLE = str.maketrans(_T2S_MAP)

def _to_simplified(text):
    """把残留的繁体常用字转成简体（仅做字符级映射，不改变粤语书面用字）。"""
    if not text:
        return text
    return text.translate(_T2S_TABLE)


def _argv_non_ascii_safe():
    """能否安全地把非 ASCII 文本经命令行参数传给 whisper-cli？

    whisper-cli 的入口是 C 的 main(int, char**)：argv 由 CRT 按系统 ANSI 代码页(ACP)
    从 Unicode 命令行转换得来，而 whisper.cpp 内部又按 UTF-8 解释 argv。
    于是当 ACP 不是 65001(UTF-8) 时——例如中文 Windows 的 936(GBK)——
    中文提示词会以 GBK 字节到达、被误读成 UTF-8，变成一串无意义的字节回退 token。
    那比不传提示词更糟（等于往上下文里灌噪声），所以这种机器上直接不传。
    这与之前「模型路径含中文导致 whisper-cli 崩溃」是同一个根因。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        return ctypes.windll.kernel32.GetACP() == 65001
    except Exception:
        return False


# ---------- GPU 后端探测 ----------
# 当前打包的 whisper-cli 默认是纯 CPU 构建（没有 ggml-cuda/vulkan 等 GPU 后端 DLL）。
# 只有目录下存在任一 GPU 后端 DLL，才说明该 whisper 构建能用显卡加速。
# 探测结果透传给前端：用户选「CPU+GPU」时，这里为 True 才会真正走 GPU；
# 否则即便选了「CPU+GPU」也只能回退到 CPU（这正是当前内置纯 CPU 版的情况）。
_GPU_BACKENDS = {
    "ggml-cuda.dll": "CUDA (NVIDIA)",
    "ggml-vulkan.dll": "Vulkan",
    "ggml-metal.dll": "Metal (Apple)",
    "ggml-clblast.dll": "OpenCL/CLBlast",
    "ggml-sycl.dll": "SYCL (Intel)",
}
def gpu_backend_available():
    """返回 (是否支持GPU, 后端名称)；基于 whisper 目录下的 GPU 后端 DLL 探测。"""
    if not WHISPER:
        return (False, None)
    wdir = os.path.dirname(os.path.abspath(WHISPER))
    for dll, name in _GPU_BACKENDS.items():
        if os.path.isfile(os.path.join(wdir, dll)):
            return (True, name)
    return (False, None)


def run_asr_full(path, lang, set1, set2, silences, gpu=True):
    """跑一次 whisper，返回全部词、命中的语气词（按两级）、以及要切除的区间。
    set1=安全级词表，set2=语境级词表。

    whisper.cpp v1.9.2 的输出约定：
      - `-owts` (=--output-words) 在 v1.9.x 里是「把词渲染到 .wts 文件/窗口」，
        找不到 monospace 字体直接失败；不再像旧版那样把 [ts --> ts] 词 行写到 stdout。
      - `-ojf` (=--output-json-full) 把完整 JSON（含每个 token 的 offsets）写到
        <wav>.json，无字体依赖、跨平台稳定。我们用它。
      - `-np` (=--no-prints) 抑制 stderr 上的 print_timings 日志。
    """
    wav = extract_wav(path)
    # whisper.cpp v1.9.2 的输出文件命名：默认时把 wav 的「全路径」+ ".json"，
    # 即 `<wav>.wav.json`（保留 .wav 后缀）。为了路径可控，我们显式传
    # `-of <不带.wav后缀的wav路径>`，让 whisper 把 json 写到 `<wav去后缀>.json`。
    wav_no_ext = os.path.splitext(wav)[0]
    json_out = wav_no_ext + ".json"
    # 按语言选模型：粤语且有粤语模型 → 用大模型；否则用通用默认（中文/英文走较快模型）
    chosen_model = select_asr_model(lang)
    model_path = _ascii_model_path(chosen_model)   # 复制到 ASCII 临时路径，规避中文/空格路径导致的 whisper 崩溃
    info = model_info(chosen_model)
    lang = (str(lang or "auto")).strip() or "auto"
    prompt, lang_note = None, None
    if lang == "yue":
        if info.get("hasYue"):
            eff_lang = "yue"
            lang_note = (f"已用粤语模型 {info.get('name')}（{info.get('tier')} 档）以粤语解码，"
                         "识别准确度较 base 模型大幅提升。")
        else:
            # 模型词表里没有 <|yue|>（99 语言词表），强行传 yue 会拿到越界/错误的语言 token，
            # 结果可能比不传更糟。这里退回 zh 硬解，再用粤语 prompt 把书面形式往粤语拉。
            eff_lang = "zh"
            lang_note = (f"当前模型 {info.get('name')}（{info.get('tier')} 档，词表 {info.get('nVocab')}）"
                         "不含粤语 token，已降级为「中文」解码 + 粤语提示词。"
                         "要真正识别粤语，请换用 ggml-large-v3-turbo.bin 或 ggml-large-v3.bin。")
        prompt = _PROMPT_YUE
    else:
        eff_lang = lang
        if lang == "zh":
            prompt = _PROMPT_ZH

    # 中文提示词只在 ACP=UTF-8 的机器上才敢传（见 _argv_non_ascii_safe 的说明）
    if prompt and not _argv_non_ascii_safe():
        prompt = None
        if lang == "yue":
            lang_note = ((lang_note + " ") if lang_note else "") + \
                ("（本机系统代码页非 UTF-8，中文粤语提示词无法安全经命令行传给 whisper-cli，已跳过提示词；"
                 "但粤语常见错写已由内置校正表兜底修正。若想让提示词也生效，可在"
                 " Windows「设置→时间和语言→语言和区域→管理语言设置→更改系统区域设置」中勾选"
                 "「Beta: 使用 Unicode UTF-8 提供全球语言支持」并重启，届时提示词会自动启用。）")

    cmd = [WHISPER, "-m", model_path, "-f", wav, "-of", wav_no_ext, "-ojf", "-np",
           # ↓↓ 关键修复 ↓↓
           # whisper-cli 默认 --max-context -1 = 无限历史文本条件化：一旦某段解码出错，
           # 错误文本会被当作上下文喂给下一段，于是自我复读、越往后越离谱。设为 0 彻底断开。
           "-mc", "0",
           # 贪心解码(-bs 1)：比 whisper 默认 beam=5 快约 5 倍，对 whisper 的识别率影响很小；
           # 大模型(尤其 large-v3-turbo)在纯 CPU 上本就很慢，这一步提速对可用性至关重要。
           # 复读崩溃由上面的 -mc 0 解决，不靠 beam 兜底。
           "-bs", "1"]
    if not gpu:
        # 单 CPU 处理：显式关闭 whisper 的 GPU 卸载（--no-gpu）。
        # 选「CPU+GPU」时不加这个参数，whisper 会优先用 GPU（前提是 whisper 构建带 GPU 后端）。
        cmd += ["-ng"]
    if eff_lang and eff_lang != "auto":
        cmd += ["-l", eff_lang]
    if prompt:
        # -mc 0 断了跨段上下文，所以要用 --carry-initial-prompt 让提示词在每段都重新前置
        cmd += ["--prompt", prompt, "--carry-initial-prompt"]
    # whisper 主进程超时：纯 CPU 跑 large 模型 + 长视频可能超过 30 分钟，放宽到 2 小时兜底，
    # 避免「处理超时」误报。真正提速靠换量化模型（q8_0/q5_0）或 GPU 后端。
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=7200)
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    raw = []  # (word, start_sec, end_sec)
    try:
        with open(json_out, "r", encoding="utf-8") as fp:
            j = json.load(fp)
    except FileNotFoundError:
        # whisper 没写出 .json——多半是 whisper-cli 崩了 / 模型加载失败 / wav 无效 / -of/-ojf 不被这个版本支持
        # 关键：先量 wav 大小，再删文件（之前先删后量会误报 -1，掩盖真实原因）
        try:
            wav_sz = os.path.getsize(wav)
        except OSError:
            wav_sz = -1
        try: os.unlink(wav)
        except OSError: pass
        try: os.unlink(json_out)
        except OSError: pass
        # 抽 stderr 里几条最可能解释根因的关键词
        s = stderr or ""
        hints = []
        for kw, why in [
            ("failed to read audio", "wav 文件无效或路径不对"),
            ("input file not found", "wav 路径不存在"),
            ("failed to initialize whisper context", "模型文件 ggml-base.bin 路径不对/损坏/不是 base 模型"),
            ("error: no input files", "wav 路径不被 whisper-cli 识别"),
            ("failed to process audio", "音频处理失败（wav 损坏）"),
        ]:
            if kw in s:
                hints.append(f"  - stderr 命中「{kw}」→{why}")
        # 若传给 whisper 的模型路径仍含非 ASCII，说明 ASCII 副本没生效，可能是复制失败
        if any(ord(ch) > 127 for ch in (model_path or "")):
            hints.append("  - 传给 whisper 的模型路径仍含非 ASCII 字符（中文/空格），whisper-cli 可能因此崩在模型加载；"
                         "ASCII 副本复制可能失败，请检查临时目录写权限")
        raise RuntimeError(
            "whisper-cli 没产出 .json 输出文件。\n"
            f"  期望输出: {json_out}\n"
            f"  wav 大小: {wav_sz} 字节\n"
            + ("\n".join(hints) + "\n" if hints else "")
            + f"  whisper 退出码: {proc.returncode}\n"
            f"  --- stderr 全文 ---\n{s if s else '(空)'}\n"
            f"  --- end stderr ---"
        )
    except Exception as e:
        try: os.unlink(wav)
        except OSError: pass
        try: os.unlink(json_out)
        except OSError: pass
        raise RuntimeError(
            f"解析 whisper 输出的 JSON 失败: {e}\n"
            f"  file={json_out}\n"
            f"  stderr 末尾:\n{(stderr or '(空)')[-500:]}"
        )
    # 解析 transcription[*].tokens[*]，每个 token 含 (offsets.from, offsets.to) 毫秒、(text) 字面
    # tokens 里 text 通常带前导空格（whisper.cpp 用前导空格标记词边界），strip 即可
    seg_texts = []  # 每个 segment 的完整文本，拼接成「文案」
    for seg in j.get("transcription", []) or []:
        seg_off = seg.get("offsets") or {}
        seg_from = seg_off.get("from", 0)
        seg_to = seg_off.get("to", 0)
        st = (seg.get("text") or "").strip()
        if st:
            seg_texts.append(st)
        for tok in seg.get("tokens", []) or []:
            t = (tok.get("text") or "").strip()
            if not t:
                continue
            off = tok.get("offsets") or {}
            s_ms = off.get("from", seg_from)
            e_ms = off.get("to", seg_to)
            raw.append((t, s_ms / 1000.0, e_ms / 1000.0))
    # 清理临时文件
    try: os.unlink(wav)
    except OSError: pass
    try: os.unlink(json_out)
    except OSError: pass
    if not raw:
        raise RuntimeError(
            "whisper JSON 里没有解析到任何 token。可能原因：\n"
            "  1) 音频太短或全是静音\n"
            "  2) 模型不对（应该用 ggml-base.bin / ggml-small.bin 等）\n"
            "  3) 语言 (-l) 设错导致模型找不到匹配 token\n"
            f"  stderr 末尾:\n{(stderr or '(空)')[-500:]}"
        )
    # 词级去复读：先把复读循环产生的重复词丢掉，避免污染字幕与语气词切点
    raw_words = [{"word": w, "start": round(s, 2), "end": round(e, 2)} for w, s, e in raw]
    words, rep_dropped = _dedup_words(raw_words)
    # 文案同样清理，并算出复读率用于质量告警
    text_clean, rep_ratio = _clean_transcript("\n".join(seg_texts))
    # 繁→简兜底：无论系统/模型吐出何种脚本，最终文案统一为简体
    text_clean = _to_simplified(text_clean)
    # 粤语输出：用校正表修掉常见错写（他們→佢哋、牽花板→天花板 等）。
    # 这一步与提示词互补——即便本机是 GBK、粤语提示词无法经 argv 送达，校正表仍能兜底大部分偏差。
    if str(lang) == "yue":
        text_clean = _correct_yue(text_clean)

    cuts = []      # 要切除的区间 [s,e,'filler']
    fillers = []   # 展示用
    for it in words:
        w, s, e = it["word"], it["start"], it["end"]
        t = _norm(w)
        tier = None
        if t in set1:
            tier = 1
        elif t in set2:
            tier = 2
        else:
            for f in set1:
                if f and (t.startswith(f) or t.endswith(f)) and len(t) <= len(f) + 1:
                    tier = 1
                    break
            if tier is None:
                for f in set2:
                    if f and (t.startswith(f) or t.endswith(f)) and len(t) <= len(f) + 1:
                        tier = 2
                        break
        if tier:
            iv = snap_interval(s, e, silences, 0.25, tier)
            if iv:
                cuts.append([iv[0], iv[1], "filler"])
                fillers.append({"word": w, "start": round(iv[0], 2), "end": round(iv[1], 2)})
    return {"words": words, "fillers": fillers, "cuts": cuts,
            "text": text_clean,
            # 识别质量诊断：复读率高 / 模型档位低 时前端会给出告警
            "repRatio": rep_ratio,
            "repDropped": rep_dropped,
            "langUsed": eff_lang,
            "langNote": lang_note,
            "model": {"name": info.get("name"), "tier": info.get("tier"),
                      "sizeMB": info.get("sizeMB"), "nVocab": info.get("nVocab"),
                      "hasYue": info.get("hasYue"), "weakForCJK": info.get("weakForCJK")}}


# ---------- 片段计算 ----------
def complement(removals, dur, pad, min_seg):
    """把若干「要剪掉的区间（带类型）」合并后求补集得到「保留区间」。
    类型：'silence' 纯静音 / 'filler' 语气词 / 'mixed' 混合。"""
    rem = []
    for r in removals:
        s, e = max(0.0, r[0]), min(dur, r[1])
        t = r[2] if len(r) > 2 else "mixed"
        if e > s:
            rem.append([s, e, t])
    rem.sort()
    merged = []
    for s, e, t in rem:
        if merged and s - merged[-1][1] < 0.02:
            merged[-1][1] = max(merged[-1][1], e)
            if merged[-1][2] != "silence" or t != "silence":
                merged[-1][2] = "mixed"
        else:
            merged.append([s, e, t])
    segs, prev = [], 0.0
    for s, e, t in merged:
        if s > prev + 1e-6:
            segs.append([prev, s])
        prev = max(prev, e)
    if prev < dur - 1e-6:
        segs.append([prev, dur])
    # cut 模式下给每段加一点边缘留白，防切掉字头字尾
    if pad:
        segs = [[max(0.0, s - pad), min(dur, e + pad)] for s, e in segs]
    # 合并因留白重叠的区间
    segs.sort()
    m = []
    for s, e in segs:
        if m and s - m[-1][1] < 0.02:
            m[-1][1] = max(m[-1][1], e)
        else:
            m.append([s, e])
    segs = [seg for seg in m if (seg[1] - seg[0]) >= min_seg]
    out = []
    for s, e in segs:
        if out and s - out[-1][1] < 0.02:
            out[-1][1] = e
        else:
            out.append([s, e])
    return out


def split_scenes(segs, silences, threshold):
    """按超过阈值的长静音把保留段分成多个场景（话题片段）。"""
    bounds = set()
    for (ss, se) in silences:
        if (se - ss) > threshold:
            bounds.add(round(se, 3))
    if not bounds:
        return [segs]
    scenes, cur = [], []
    for (s, e) in segs:
        if cur and any(abs(b - s) < 0.3 for b in bounds):
            scenes.append(cur)
            cur = [(s, e)]
        else:
            cur.append((s, e))
    if cur:
        scenes.append(cur)
    return [sc for sc in scenes if sc]


def is_silence_gap(gs, ge, silences):
    for (ss, se) in silences:
        if ss <= gs + 0.02 and se >= ge - 0.02:
            return True
    return False


# ---------- 字幕生成（SRT / VTT）----------
def _is_cjk(ch):
    o = ord(ch)
    return (0x3000 <= o <= 0x303F) or (0x3400 <= o <= 0x4DBF) or \
           (0x4E00 <= o <= 0x9FFF) or (0xF900 <= o <= 0xFAFF) or \
           (0xFF00 <= o <= 0xFFEF)


def _join_words(words):
    """把词列表拼成自然文本：CJK 相邻不加空格，仅两个非 CJK 词之间加空格。"""
    txt = ""
    for w in words:
        w0 = (w or "").strip()
        if not w0:
            continue
        if txt and not _is_cjk(txt[-1]) and not _is_cjk(w0[0]) and not txt[-1].isspace():
            txt += " "
        txt += w0
    return txt


def _ts_srt(sec):
    sec = max(0.0, float(sec)); ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ts_vtt(sec):
    sec = max(0.0, float(sec)); ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _build_timeline(blocks):
    """把渲染用的 blocks（('speech'/'gap', 原片起, 原片止)）映射成成品时间轴。
    返回 (entries, total)：entries 为每条 speech block 的 (原片起, 原片止, 成品起)，
    total 为成品总时长。gap block 仅推进时间轴（压短后的停顿长度）。"""
    entries, cut = [], 0.0
    for typ, s, e in blocks:
        if typ == "speech":
            entries.append((s, e, cut))
            cut += (e - s)
        else:
            cut += (e - s)
    return entries, cut


def _map_time(entries, t):
    for s, e, cs in entries:
        if s - 1e-6 <= t <= e + 1e-6:
            return cs + (t - s)
    return None


def _build_cues(surv):
    """surv: 成品时间轴上的词列表 [{start,end,word}]（按时间排序）。
    按停顿间隔(>1.2s) / 词数(>=14) / 字数(>=42) 切分字幕行。"""
    cues, cur = [], []
    for x in surv:
        if cur:
            gap = x["start"] - cur[-1]["end"]
            clen = sum(len(z["word"]) for z in cur)
            if gap > 1.2 or len(cur) >= 14 or clen >= 42:
                cues.append(cur); cur = []
        cur.append(x)
    if cur:
        cues.append(cur)
    out = []
    for c in cues:
        out.append((c[0]["start"], c[-1]["end"], _join_words(z["word"] for z in c)))
    return out


def _make_srt(cues):
    lines = []
    for i, (s, e, t) in enumerate(cues, 1):
        lines += [str(i), f"{_ts_srt(s)} --> {_ts_srt(e)}", t, ""]
    return "\n".join(lines)


def _make_vtt(cues):
    lines = ["WEBVTT", ""]
    for s, e, t in cues:
        lines += [f"{_ts_vtt(s)} --> {_ts_vtt(e)}", t, ""]
    return "\n".join(lines)


def scene_subtitles(words, blocks, lang=None):
    """给定单个场景的渲染 blocks 与该视频全部词（原片时间戳），
    返回 (srt, vtt, transcript)：仅保留落在保留语音段内的词，并把时间戳映射到成品时间轴。
    lang=='yue' 时对字幕文本与文案应用粤语校正表。
    words 为 None 时返回 (None, None, None)。"""
    if not words:
        return None, None, None
    entries, _ = _build_timeline(blocks)
    if not entries:
        return None, None, None
    surv = []
    for w in words:
        cs = _map_time(entries, w.get("start"))
        ce = _map_time(entries, w.get("end"))
        if cs is not None and ce is not None:
            surv.append({"start": cs, "end": ce, "word": w.get("word", "")})
    if not surv:
        return None, None, None
    transcript = _to_simplified(_join_words(x["word"] for x in surv))
    cues = _build_cues(surv)
    if str(lang) == "yue":
        transcript = _correct_yue(transcript)
        cues = [(s, e, _correct_yue(t)) for (s, e, t) in cues]
    return _make_srt(cues), _make_vtt(cues), transcript


# ---------- 编码档位 ----------
QUALITY_PRESETS = {
    # preset=medium/veryfast/superfast，crf 越小画质越高（0=数学无损，18=视觉无损，23=默认，28=明显损失）
    # aac 音频码率（kbps），128=语音够用，160/192=高质量
    "high":   {"preset": "medium",   "crf": "18", "audio_kbps": "192k"},  # 默认：接近原片
    "medium": {"preset": "medium",   "crf": "23", "audio_kbps": "160k"},  # 均衡
    "fast":   {"preset": "veryfast", "crf": "28", "audio_kbps": "128k"},  # 高速小文件
}


# ---------- 剪辑渲染 ----------
def render_blocks(input_path, blocks, audio, quality="high"):
    """blocks: 交替的 ('speech',s,e) / ('gap',s,e)。gap 为保留的短静音（来自原片静音区）。
    quality: 'high' / 'medium' / 'fast'（见 QUALITY_PRESETS）。"""
    out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    v, a = [], []
    n = 0
    for typ, s, e in blocks:
        v.append(f"[0:v]trim={s:.3f}:{e:.3f},setpts=PTS-STARTPTS[v{n}]")
        a.append(f"[0:a]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS[a{n}]")
        n += 1
    if n == 0:
        return None, blocks
    vc = "".join(f"[v{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=0[outv]"
    ac = "".join(f"[a{k}]" for k in range(n)) + f"concat=n={n}:v=0:a=1[outa]"
    fc = ";".join(v + a + [vc, ac])
    q = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["high"])
    cmd = [FFMPEG, "-i", input_path, "-filter_complex", fc]
    if audio:
        cmd += ["-map", "[outv]", "-map", "[outa]"]
    else:
        cmd += ["-map", "[outv]", "-an"]
    # 编码参数：
    #   crf 越小画质越高（0=数学无损、18=视觉无损、23=libx264 默认、28=明显损失）
    #   preset medium 比 veryfast 压缩效率高很多（同画质码率更低；veryfast 适合极速试切）
    #   -pix_fmt yuv420p：保证 Windows Media Player / 微信 / 浏览器都能播
    #   -movflags +faststart：moov 原子前置，浏览器/微信预览可边下边播
    cmd += ["-c:v", "libx264", "-preset", q["preset"], "-crf", q["crf"],
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", q["audio_kbps"],
            "-movflags", "+faststart",
            "-y", out]
    subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
    return out, blocks


def render_scene(input_path, scene_segs, silences, mode, floor, audio, quality="high"):
    blocks = []
    for i, (s, e) in enumerate(scene_segs):
        blocks.append(("speech", s, e))
        if i < len(scene_segs) - 1:
            gs, ge = e, scene_segs[i + 1][0]
            # 压缩模式：在纯静音间隙保留一个被压短的小停顿；切除模式直接不留
            if mode == "compress" and is_silence_gap(gs, ge, silences):
                kept = min(ge - gs, floor)
                if kept > 0.02:
                    blocks.append(("gap", gs, gs + kept))
    out, blocks = render_blocks(input_path, blocks, audio, quality)
    return out, blocks


def do_cut_plan(input_path, scenes, silences, mode, floor, audio, quality="high", words=None, lang=None):
    """渲染全部场景并生成字幕。
    单个场景返回 ('single', mp4路径, srt, vtt, transcript)；
    多个场景返回 ('zip', zip路径)，zip 内含每个片段的 mp4 + srt + vtt + 文案.txt。
    words 为 None（未做语音识别）时不出字幕。"""
    if len(scenes) == 1:
        out, blocks = render_scene(input_path, scenes[0], silences, mode, floor, audio, quality)
        if not out:
            return ("single", None, None, None, None)
        srt, vtt, txt = scene_subtitles(words, blocks, lang)
        return ("single", out, srt, vtt, txt)
    parts = []  # (base, mp4_path, srt, vtt, txt)
    for i, sc in enumerate(scenes):
        out, blocks = render_scene(input_path, sc, silences, mode, floor, audio, quality)
        if out:
            base = f"口播片段_{i + 1:02d}"
            srt, vtt, txt = scene_subtitles(words, blocks, lang)
            parts.append((base, out, srt, vtt, txt))
    zp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False).name
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for base, out, srt, vtt, txt in parts:
            z.write(out, base + ".mp4")
            if srt:
                z.writestr(base + ".srt", srt)
            if vtt:
                z.writestr(base + ".vtt", vtt)
            if txt:
                z.writestr(base + "_文案.txt", txt)
    for _, out, _, _, _ in parts:
        try:
            os.unlink(out)
        except Exception:
            pass
    return ("zip", zp)


# ---------- 统一规划（analyze / cut 共用）----------
def compute_plan(input_path, params):
    if bool(params.get("filler")) and (not WHISPER or not WHISPER_MODEL):
        raise RuntimeError("未找到 whisper 语音识别。请确认本程序目录下的 whisper\\ 子文件夹含 whisper-cli.exe 与 models\\ggml-*.bin。")
    thr = float(params.get("thr", -28))
    dmin = float(params.get("min", 0.4))
    silences = run_silencedetect(input_path, thr, dmin)
    dur = duration_of(input_path)
    audio = has_audio(input_path)
    filler = bool(params.get("filler"))
    set1 = parse_set(params.get("fillerWords"))
    set2 = parse_set(params.get("fillerWords2"))
    lang = str(params.get("lang", "auto")) or "auto"

    fillers, filler_count, words = [], 0, []
    asr = {}
    removals = [[s, e, "silence"] for s, e in silences]
    if filler:
        use_gpu = str(params.get("gpu", "gpu")).lower() != "cpu"
        asr = run_asr_full(input_path, lang, set1, set2, silences, gpu=use_gpu)
        words = asr["words"]
        fillers = asr["fillers"]
        filler_count = len(asr["cuts"])
        removals += asr["cuts"]

    mode = params.get("pauseMode", "cut")
    pad = 0.0 if mode == "compress" else float(params.get("pad", 0.1))
    keep = complement(removals, dur, pad, float(params.get("minSeg", 0.3)))

    slice_mode = bool(params.get("sliceMode"))
    slice_threshold = float(params.get("sliceThreshold", 2.0))
    scenes = [keep] if not slice_mode else split_scenes(keep, silences, slice_threshold)

    return dict(dur=dur, audio=audio, silences=silences, keep=keep, removals=removals,
                fillers=fillers, filler_count=filler_count, words=words, filler=filler,
                text=asr.get("text") if filler else None,
                # 识别质量诊断，透传给前端做告警
                asr_diag=({"repRatio": asr.get("repRatio", 0.0),
                           "repDropped": asr.get("repDropped", 0),
                           "langUsed": asr.get("langUsed"),
                           "langNote": asr.get("langNote"),
                           "model": asr.get("model") or {}} if filler else None),
                mode=mode, floor=float(params.get("floorGap", 0.25)),
                scenes=scenes, slice_mode=slice_mode, slice_threshold=slice_threshold,
                quality=str(params.get("quality", "high") or "high"),
                # 语言透传给 do_cut_plan，使其能对字幕/文案应用粤语校正表
                lang=lang)


# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if self.protocol_version == "HTTP/1.0":
            self.send_header("Connection", "close")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except Exception:
            pass

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            try:
                with open(HTML_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif self.path == "/api/ffmpeg":
            if FFMPEG:
                self._send(200, json.dumps({"ok": True, "bin": FFMPEG}))
            else:
                self._send(200, json.dumps({"ok": False,
                    "error": "未找到 ffmpeg.exe。请确认本程序目录下的 ffmpeg\\ 子文件夹完整（ffmpeg.exe + ffprobe.exe + 相关 dll）。"}))
        elif self.path == "/api/asr-status":
            refresh_whisper()
            gpu_ok, gpu_name = gpu_backend_available()
            mi = model_info(WHISPER_MODEL) if WHISPER_MODEL else {}
            ymi = model_info(WHISPER_YUE_MODEL) if WHISPER_YUE_MODEL else {}
            self._send(200, json.dumps({
                "whisper": bool(WHISPER),
                "whisperBin": WHISPER,
                "model": bool(WHISPER_MODEL),
                "modelName": os.path.basename(WHISPER_MODEL) if WHISPER_MODEL else None,
                # 模型档位与粤语能力，前端据此提示用户是否需要换模型
                "tier": mi.get("tier"),
                "sizeMB": mi.get("sizeMB"),
                "nVocab": mi.get("nVocab"),
                "hasYue": mi.get("hasYue"),
                "weakForCJK": mi.get("weakForCJK"),
                # 粤语专用模型（含粤语 token 的高档模型），与通用默认模型分开
                "yueSupported": bool(WHISPER_YUE_MODEL),
                "yueModelName": os.path.basename(WHISPER_YUE_MODEL) if WHISPER_YUE_MODEL else None,
                "yueTier": ymi.get("tier"),
                # GPU 后端探测：当前内置 whisper 是纯 CPU 构建，gpuAvailable 会是 False
                "gpuAvailable": gpu_ok,
                "gpuBackend": gpu_name,
                # 粤语提示词能否在本机送达 whisper-cli（仅系统为 UTF-8 代码页时可行；GBK 下经 argv 会乱码，已用校正表兜底）
                "promptActive": _argv_non_ascii_safe(),
            }))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")

        params = {}
        input_path = None
        is_upload = False
        if ctype.startswith("application/json"):
            data = json.loads(raw.decode("utf-8"))
            input_path = data.get("path")
            params = data
        else:
            is_upload = True
            xparams = self.headers.get("X-Params")
            params = json.loads(xparams) if xparams else {}
            try:
                with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
                    tf.write(raw)
                    input_path = tf.name
            except Exception as e:
                self._send(500, json.dumps({"error": "保存上传文件失败：" + str(e)}))
                return

        if not input_path or not os.path.isfile(input_path):
            self._send(400, json.dumps({"error": "视频文件不存在：" + str(input_path)}))
            return

        try:
            if self.path == "/api/analyze":
                refresh_whisper()
                plan = compute_plan(input_path, params)
                resp = {
                    "origDur": plan["dur"],
                    "segments": [[round(a, 3), round(b, 3)] for a, b in plan["keep"]],
                    "silences": len(plan["silences"]),
                    "filler": plan["filler"],
                    "sceneCount": len(plan["scenes"]),
                }
                if plan["filler"]:
                    resp["fillers"] = plan["fillers"][:200]
                    resp["fillerCount"] = plan["filler_count"]
                    resp["words"] = plan["words"][:600]
                    resp["transcript"] = plan.get("text") or ""
                    resp["asrDiag"] = plan.get("asr_diag") or {}
                self._send(200, json.dumps(resp))

            elif self.path == "/api/asr":
                if not WHISPER or not WHISPER_MODEL:
                    self._send(400, json.dumps({"error": "未找到 whisper 语音识别。"}))
                    return
                silences = run_silencedetect(input_path,
                                            float(params.get("thr", -28)),
                                            float(params.get("min", 0.4)))
                use_gpu = str(params.get("gpu", "gpu")).lower() != "cpu"
                asr = run_asr_full(input_path, str(params.get("lang", "auto")) or "auto",
                                   parse_set(params.get("fillerWords")),
                                   parse_set(params.get("fillerWords2")), silences, gpu=use_gpu)
                self._send(200, json.dumps({
                    "fillers": asr["fillers"][:200],
                    "fillerCount": len(asr["cuts"]),
                    "words": asr["words"][:600],
                    "transcript": asr.get("text") or "",
                    "asrDiag": {"repRatio": asr.get("repRatio", 0.0),
                                "repDropped": asr.get("repDropped", 0),
                                "langUsed": asr.get("langUsed"),
                                "langNote": asr.get("langNote"),
                                "model": asr.get("model") or {}},
                }))

            elif self.path == "/api/cut":
                refresh_whisper()
                plan = compute_plan(input_path, params)
                if not plan["keep"]:
                    self._send(400, json.dumps({"error": "未检测到可保留的语音内容，请检查静音阈值是否过低。"}))
                    return
                words = plan.get("words")
                has_sub = bool(plan.get("filler")) and bool(words)
                kind, outpath, srt, vtt, txt = do_cut_plan(
                    input_path, plan["scenes"], plan["silences"], plan["mode"],
                    plan["floor"], plan["audio"], plan.get("quality", "high"), words,
                    lang=plan.get("lang"))
                if not outpath or not os.path.isfile(outpath):
                    self._send(500, json.dumps({"error": "剪辑渲染失败，请重试或调高静音阈值。"}))
                    return
                with open(outpath, "rb") as f:
                    data = f.read()
                os.unlink(outpath)
                if is_upload:
                    try:
                        os.unlink(input_path)
                    except Exception:
                        pass
                # 启用语音识别且确有字幕内容时，把「视频 + srt + vtt + 文案」打包成 zip 交付
                wrap = has_sub and (srt or vtt or txt)
                if kind == "single" and wrap:
                    zp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False).name
                    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
                        z.writestr("kouchao_cut.mp4", data)
                        if srt:
                            z.writestr("kouchao_cut.srt", srt)
                        if vtt:
                            z.writestr("kouchao_cut.vtt", vtt)
                        if txt:
                            z.writestr("文案.txt", txt)
                    with open(zp, "rb") as f:
                        data = f.read()
                    os.unlink(zp)
                    self._send(200, data, "application/zip",
                               {"Content-Disposition": 'attachment; filename="kouchao_cut.zip"'})
                elif kind == "single":
                    self._send(200, data, "video/mp4",
                               {"Content-Disposition": 'attachment; filename="kouchao_cut.mp4"'})
                else:
                    self._send(200, data, "application/zip",
                               {"Content-Disposition": 'attachment; filename="kouchao_cut.zip"'})
            else:
                self._send(404, json.dumps({"error": "unknown endpoint"}))
        except subprocess.TimeoutExpired:
            self._send(500, json.dumps({"error": "处理超时（文件可能过大或模型太慢）"}))
        except RuntimeError as e:
            self._send(400, json.dumps({"error": str(e)}))
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}))


def _open_browser(url):
    # 等服务真正起来再开浏览器
    for _ in range(20):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.3):
                break
        except OSError:
            import time
            time.sleep(0.3)
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    # 控制台防御：中文 Windows 的控制台默认 GBK，遇到 GBK 编不出的字符（如 ⚠ U+26A0、emoji）
    # print() 会抛 UnicodeEncodeError，直接把整个程序打挂。这里把编码错误降级为替换字符，
    # 保证任何输出都不会成为崩溃点。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://127.0.0.1:{PORT}/"
    print("=" * 56)
    print("  口播自动剪辑器（便携版）")
    print("=" * 56)
    print(f"  已在浏览器打开：{url}")
    print(f"  ffmpeg : {FFMPEG if FFMPEG else '未找到（去静音功能不可用）'}")
    print(f"  whisper: {WHISPER if WHISPER else '未找到（语气词切除不可用）'}")
    _gpu_ok, _gpu_name = gpu_backend_available()
    print(f"  GPU加速 : {('支持（' + _gpu_name + '）' if _gpu_ok else '当前 whisper 为纯 CPU 构建，CPU+GPU 模式将回退到 CPU')}")
    print(f"  粤语提示词: {('生效（系统为 UTF-8，已传给 whisper）' if _argv_non_ascii_safe() else '未启用（系统 GBK，中文提示词经命令行会乱码；已用粤语校正表兜底，开启系统 Beta UTF-8 即可启用）')}")
    if WHISPER_MODEL:
        _mi = model_info()
        print(f"  model  : {_mi.get('name')}  [{_mi.get('tier')} 档 / {_mi.get('sizeMB')}MB / 词表 {_mi.get('nVocab')}]")
        print(f"  粤语    : {'支持（模型含 yue token）' if _mi.get('hasYue') else '不支持——需 ggml-large-v3-turbo.bin 或 ggml-large-v3.bin'}")
        if _mi.get("weakForCJK"):
            print(f"  [提示] : {_mi.get('tier')} 档模型对中文/粤语口播偏弱，建议换 medium 以上；粤语建议 large-v3 系。")
    else:
        print("  model  : 未找到")
    print("-" * 56)
    print("  关闭本窗口即可退出程序。")
    print("=" * 56)
    threading.Timer(1.0, _open_browser, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
