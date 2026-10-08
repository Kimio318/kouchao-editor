# Copyright (c) 2026 Kimio318
# SPDX-License-Identifier: MIT
# KouchaoEditor（口播剪辑器）—— 自有源码，采用 MIT 许可证（详见 LICENSE）。

"""集成测试：单段/多段场景走完整 渲染+字幕+zip 流水线。"""
import os, sys, zipfile, io
os.environ["FFMPEG_BIN"] = r"C:\Users\admin\WorkBuddy\2026-08-18-14-57-43\KouchaoEditor\ffmpeg\ffmpeg.exe"
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
sys.path.insert(0, SRC)
import kouchao_server as ks

CLIP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_clip.mp4")
ok = 0; fail = 0
def check(n, c, extra=""):
    global ok, fail
    if c: ok += 1; print("  PASS", n)
    else: fail += 1; print("  FAIL", n, extra)

# 合成词（落在 0-3s 内的两段，中间 1.5s 长停顿 -> 两行字幕）
words = [
    {"start":0.2,"end":0.8,"word":"我们"},
    {"start":0.9,"end":1.3,"word":"今天"},
    {"start":2.8,"end":3.0,"word":"去公园"},
]

print("== A. 单段场景 -> single + 字幕 ==")
kind, out, srt, vtt, txt = ks.do_cut_plan(CLIP, [[[0.0,3.0]]], [], "cut", 0.25, True, "fast", words)
check("kind=single", kind=="single", kind)
check("视频生成", out and os.path.isfile(out))
check("srt 非空且含 -->", srt and "-->" in srt)
check("vtt 头", vtt and vtt.startswith("WEBVTT"))
check("txt=我们今天去公园", txt=="我们今天去公园", repr(txt))
if out and os.path.isfile(out): os.unlink(out)

print("== B. 单段场景 -> 打包 zip(视频+字幕+文案) 模拟 /api/cut 包裹 ==")
# 复刻 /api/cut 的 wrap 逻辑：启用语音识别且有字幕内容时包成 zip
kind, out, srt, vtt, txt = ks.do_cut_plan(CLIP, [[[0.0,3.0]]], [], "cut", 0.25, True, "fast", words)
with open(out,"rb") as f: data=f.read()
os.unlink(out)
wrap = bool(srt or vtt or txt)
check("wrap 为真", wrap)
if wrap:
    import tempfile
    zp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False).name
    with zipfile.ZipFile(zp,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("kouchao_cut.mp4", data)
        if srt: z.writestr("kouchao_cut.srt", srt)
        if vtt: z.writestr("kouchao_cut.vtt", vtt)
        if txt: z.writestr("文案.txt", txt)
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
    check("zip 含 mp4", "kouchao_cut.mp4" in names, names)
    check("zip 含 srt", "kouchao_cut.srt" in names)
    check("zip 含 vtt", "kouchao_cut.vtt" in names)
    check("zip 含 文案.txt", "文案.txt" in names)
    os.unlink(zp)

print("== C. 多段场景 -> zip 含各片段 mp4+srt+vtt+文案 ==")
kind, zp = ks.do_cut_plan(CLIP, [[[0.0,1.4]], [[1.6,3.0]]], [], "cut", 0.25, True, "fast", words)
check("kind=zip", kind=="zip", kind)
check("zip 存在", zp and os.path.isfile(zp))
if zp and os.path.isfile(zp):
    with zipfile.ZipFile(zp) as z:
        names = sorted(z.namelist())
    print("    zip 内:", names)
    check("含 口播片段_01.mp4", "口播片段_01.mp4" in names)
    check("含 口播片段_01.srt", "口播片段_01.srt" in names)
    check("含 口播片段_01.vtt", "口播片段_01.vtt" in names)
    check("含 口播片段_01_文案.txt", "口播片段_01_文案.txt" in names)
    os.unlink(zp)

print("== D. 无非空 words -> 不出字幕 ==")
kind, out, srt, vtt, txt = ks.do_cut_plan(CLIP, [[[0.0,3.0]]], [], "cut", 0.25, True, "fast", None)
check("srt 为 None", srt is None)
check("vtt 为 None", vtt is None)
check("txt 为 None", txt is None)
if out and os.path.isfile(out): os.unlink(out)

print(f"\n结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
