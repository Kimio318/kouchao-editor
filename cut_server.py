#!/usr/bin/env python3
# 口播自动剪辑器 —— 本地原生版（调用本机 ffmpeg.exe + whisper.cpp 语音识别）
# 仅用 Python 标准库；视频全程本地处理，不上传。
import os, re, sys, json, shutil, glob, tempfile, subprocess, html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("CUT_PORT", 8011))
HERE = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(HERE, "kouchao_native.html")

# ---------- 定位 ffmpeg ----------
def find_ffmpeg():
    if os.environ.get("FFMPEG_BIN") and os.path.isfile(os.environ["FFMPEG_BIN"]):
        return os.environ["FFMPEG_BIN"]
    p = shutil.which("ffmpeg")
    if p:
        return p
    cands = [
        r"E:\口播测试\ffmpeg.exe",
        r"E:\口播测试\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\ffmpeg\ffmpeg.exe",
        os.path.join(HERE, "ffmpeg.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    try:
        for root, _, files in os.walk(r"E:\口播测试"):
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
    p = shutil.which("whisper-cli") or shutil.which("whisper")
    if p:
        return p
    cands = [
        r"E:\口播测试\whisper\whisper-cli.exe",
        r"E:\口播测试\whisper.exe",
        os.path.join(HERE, "whisper-cli.exe"),
        os.path.join(HERE, "whisper.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    try:
        for root, _, files in os.walk(r"E:\口播测试"):
            for f in files:
                if f.lower() in ("whisper-cli.exe", "whisper.exe"):
                    return os.path.join(root, f)
    except Exception:
        pass
    return None

def find_whisper_model():
    if os.environ.get("WHISPER_MODEL") and os.path.isfile(os.environ["WHISPER_MODEL"]):
        return os.environ["WHISPER_MODEL"]
    pats = [
        r"E:\口播测试\whisper\models\ggml-*.bin",
        r"E:\口播测试\whisper\*.bin",
        os.path.join(HERE, "models", "ggml-*.bin"),
        os.path.join(HERE, "*.bin"),
    ]
    for pat in pats:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    try:
        for root, _, files in os.walk(r"E:\口播测试"):
            for f in files:
                if f.lower().startswith("ggml-") and f.lower().endswith(".bin"):
                    return os.path.join(root, f)
    except Exception:
        pass
    return None

WHISPER = find_whisper()
WHISPER_MODEL = find_whisper_model()

def refresh_whisper():
    """每次请求重新探测 whisper 位置（放好文件后无需重启服务，刷新页面即可）"""
    global WHISPER, WHISPER_MODEL
    WHISPER = find_whisper()
    WHISPER_MODEL = find_whisper_model()

# ---------- ffmpeg 辅助 ----------
def duration_of(path):
    if not FFPROBE:
        return 0.0
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60)
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
            capture_output=True, text=True, timeout=60)
        return bool(out.stdout.strip())
    except Exception:
        return True

def run_silencedetect(path, noise, d):
    """返回静音区间列表 [[start,end],...]（秒）"""
    cmd = [FFMPEG, "-i", path,
           "-af", f"silencedetect=noise={noise}dB:d={d}",
           "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    silences = []
    cur = None
    for line in proc.stderr.splitlines():
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
def segments_from_cuts(cuts, duration, pad, min_seg):
    """把若干「要剪掉的区间」合并后，求补集得到「保留区间」"""
    if not cuts:
        return [[0.0, duration]]
    # 合并相邻/重叠的剪切区间
    cuts = [[max(0.0, s), min(duration, e)] for s, e in cuts if e > s]
    cuts.sort()
    merged = []
    for c in cuts:
        if merged and c[0] - merged[-1][1] < 0.02:
            merged[-1][1] = max(merged[-1][1], c[1])
        else:
            merged.append(c[:])
    # 反推保留区间
    segs, prev = [], 0.0
    for s, e in merged:
        if s > prev:
            segs.append([prev, s])
        prev = max(prev, e)
    if prev < duration:
        segs.append([prev, duration])
    # 加留白
    segs = [[max(0.0, s - pad), min(duration, e + pad)] for s, e in segs]
    # 过滤过短
    segs = [seg for seg in segs if (seg[1] - seg[0]) >= min_seg]
    # 合并相邻保留段
    out = []
    for seg in segs:
        if out and seg[0] - out[-1][1] < 0.02:
            out[-1][1] = seg[1]
        else:
            out.append(seg[:])
    return out

# ---------- 语音识别（whisper.cpp）----------
def extract_wav(path):
    wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    cmd = [FFMPEG, "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
           "-f", "wav", wav]
    subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return wav

def hms_to_sec(t):
    # 支持 HH:MM:SS.mmm 或 SS.mmm
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
    """解析 whisper.cpp 打印的 [start --> end] 文字 行，返回 [(text,start,end)]"""
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

def run_asr(path, lang, filler_set):
    """返回 {words, fillers:[(word,start,end)], cuts:[[s,e]]}。
    只使用稳定存在的标志（-m/-f/-l/-ml 1），解析 stdout 的时间戳行，
    不依赖 JSON 输出标志（不同版本名称不一致）。"""
    wav = extract_wav(path)
    # -owts (=--output-words) 干净开启「词级时间戳」，stdout 输出
    #   [00:00:00.000 --> 00:00:00.500]  嗯
    # 这种行，解析器据此定位每个词的时间。不要用 -ml（那是 max-len，会把句子拆成 1 token）。
    cmd = [WHISPER, "-m", WHISPER_MODEL, "-f", wav, "-owts"]
    if lang and lang != "auto":
        cmd += ["-l", lang]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    os.unlink(wav)
    # 同时看 stdout 与 stderr（进度/结果可能落在任一处）
    words = parse_timestamped_lines(proc.stdout + "\n" + proc.stderr)
    fillers, cuts = [], []
    norm = lambda s: re.sub(r"[\s，。、！？；：,.!?;:\"'`]+", "", s or "")
    for w, s, e in words:
        t = norm(w)
        # 精确匹配（词级时间戳下，口头禅多为独立成词）→ 直接整词切除
        hit = t in filler_set
        # 边界匹配兜底：整词以/以 filler 开头或结尾，且长度只比 filler 多 ≤1 字
        # 例：「那个呢」→ 切；「其实这个功能挺好用的」(多 7 字) → 不切，避免误伤内容
        if not hit:
            for f in filler_set:
                if f and (t.startswith(f) or t.endswith(f)) and len(t) <= len(f) + 1:
                    hit = True
                    break
        if hit:
            fillers.append((w, s, e))
            cuts.append([max(0.0, s - 0.12), e + 0.12])
    return {"words": words, "fillers": fillers, "cuts": cuts}

# ---------- 剪辑 ----------
def build_filter(segments):
    n = len(segments)
    v, a = [], []
    for k, (s, e) in enumerate(segments):
        v.append(f"[0:v]trim={s:.3f}:{e:.3f},setpts=PTS-STARTPTS[v{k}]")
        a.append(f"[0:a]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS[a{k}]")
    vc = "".join(f"[v{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=0[outv]"
    ac = "".join(f"[a{k}]" for k in range(n)) + f"concat=n={n}:v=0:a=1[outa]"
    return ";".join(v + a + [vc, ac])

def do_cut(path, segments, audio):
    out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    fc = build_filter(segments)
    cmd = [FFMPEG, "-i", path, "-filter_complex", fc]
    if audio:
        cmd += ["-map", "[outv]", "-map", "[outa]"]
    else:
        cmd += ["-map", "[outv]", "-an"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-y", out]
    subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    return out

# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # 每个请求后关闭连接，避免 keep-alive 下大 body 缓冲丢失

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")  # 允许从预览面板 / file:// 跨域调用
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
                    "error": "未找到 ffmpeg.exe。请把它放到 E:\\口播测试\\ 或 PATH，或设置 FFMPEG_BIN 环境变量。"}))
        elif self.path == "/api/asr-status":
            refresh_whisper()
            self._send(200, json.dumps({
                "whisper": bool(WHISPER),
                "whisperBin": WHISPER,
                "model": bool(WHISPER_MODEL),
                "modelName": os.path.basename(WHISPER_MODEL) if WHISPER_MODEL else None,
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
            filler = bool(params.get("filler", False))
            filler_words = [w.strip() for w in str(params.get("fillerWords", "")).replace("，", ",").split(",") if w.strip()]
            lang = str(params.get("lang", "auto")) or "auto"

            if self.path == "/api/analyze":
                refresh_whisper()
                dur = duration_of(input_path)
                silences = run_silencedetect(input_path,
                                            float(params.get("thr", -28)),
                                            float(params.get("min", 0.4)))
                resp = {"origDur": dur, "segments": [], "silences": len(silences),
                        "filler": filler}
                if filler:
                    if not WHISPER or not WHISPER_MODEL:
                        self._send(400, json.dumps({"error":
                            "未找到 whisper 语音识别。请下载 whisper-cli.exe 与 ggml 模型放到 E:\\口播测试\\whisper\\ ，详见页面底部说明。"}))
                        return
                    asr = run_asr(input_path, lang, set(filler_words))
                    cuts = silences + asr["cuts"]
                    resp["fillers"] = [{"word": w, "start": round(s, 2), "end": round(e, 2)}
                                       for w, s, e in asr["fillers"][:200]]
                    resp["fillerCount"] = len(asr["fillers"])
                    resp["words"] = [{"word": w, "start": round(s, 2), "end": round(e, 2)}
                                     for w, s, e in asr["words"][:600]]
                else:
                    cuts = silences
                segs = segments_from_cuts(cuts, dur,
                                          float(params.get("pad", 0.1)),
                                          float(params.get("minSeg", 0.3)))
                resp["segments"] = segs
                self._send(200, json.dumps(resp))

            elif self.path == "/api/asr":
                if not WHISPER or not WHISPER_MODEL:
                    self._send(400, json.dumps({"error": "未找到 whisper 语音识别。"}))
                    return
                asr = run_asr(input_path, lang, set(filler_words))
                self._send(200, json.dumps({
                    "fillers": [{"word": w, "start": round(s, 2), "end": round(e, 2)}
                                for w, s, e in asr["fillers"][:200]],
                    "fillerCount": len(asr["fillers"]),
                    "words": [{"word": w, "start": round(s, 2), "end": round(e, 2)}
                              for w, s, e in asr["words"][:600]],
                }))

            elif self.path == "/api/cut":
                refresh_whisper()
                segments = params.get("segments")
                if filler:
                    if not WHISPER or not WHISPER_MODEL:
                        self._send(400, json.dumps({"error": "未找到 whisper 语音识别。"}))
                        return
                    silences = run_silencedetect(input_path,
                                                float(params.get("thr", -28)),
                                                float(params.get("min", 0.4)))
                    asr = run_asr(input_path, lang, set(filler_words))
                    cuts = silences + asr["cuts"]
                    segments = segments_from_cuts(cuts, duration_of(input_path),
                                                  float(params.get("pad", 0.1)),
                                                  float(params.get("minSeg", 0.3)))
                elif not segments:
                    silences = run_silencedetect(input_path,
                                                float(params.get("thr", -28)),
                                                float(params.get("min", 0.4)))
                    segments = segments_from_cuts(silences, duration_of(input_path),
                                                  float(params.get("pad", 0.1)),
                                                  float(params.get("minSeg", 0.3)))
                audio = has_audio(input_path)
                out = do_cut(input_path, segments, audio)
                with open(out, "rb") as f:
                    data = f.read()
                os.unlink(out)
                if is_upload:
                    try: os.unlink(input_path)
                    except Exception: pass
                self._send(200, data, "video/mp4",
                           {"Content-Disposition": 'attachment; filename="kouchao_cut.mp4"'})
            else:
                self._send(404, json.dumps({"error": "unknown endpoint"}))
        except subprocess.TimeoutExpired:
            self._send(500, json.dumps({"error": "处理超时（文件可能过大或模型太慢）"}))
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}))

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"口播剪辑器(本地原生版) 已启动: http://127.0.0.1:{PORT}/")
    print(f"ffmpeg: {FFMPEG if FFMPEG else '未找到'}")
    print(f"whisper: {WHISPER if WHISPER else '未找到'}")
    print(f"whisper model: {WHISPER_MODEL if WHISPER_MODEL else '未找到'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("已停止")
