"""字幕生成单元测试：验证 _build_cues 修复 + scene_subtitles 端到端。"""
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
sys.path.insert(0, SRC)

import kouchao_server as ks

ok = 0
fail = 0

def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name}  {extra}")

# ---------------------------------------------------------------------------
print("== 1. _build_cues 停顿切分（修复验证）==")
# 原文时间轴词：我们(0.5-1.2) 今天(1.5-2.2) 去(4.45-5.05) 公园(5.25-6.05)
# 今天->去 之间有 2.25s 停顿(>1.2) 应切成两行
surv = [
    {"start": 0.5, "end": 1.2, "word": "我们"},
    {"start": 1.5, "end": 2.2, "word": "今天"},
    {"start": 4.45, "end": 5.05, "word": "去"},
    {"start": 5.25, "end": 6.05, "word": "公园"},
]
cues = ks._build_cues(surv)
check("切成 2 行", len(cues) == 2, f"得到 {len(cues)} 行")
if len(cues) == 2:
    check("第一行=我们今天", cues[0][2] == "我们今天", f"得到 {cues[0][2]!r}")
    check("第二行=去公园", cues[1][2] == "去公园", f"得到 {cues[1][2]!r}")
    check("第一行结束<=第二行开始", cues[0][1] <= cues[1][0])

# ---------------------------------------------------------------------------
print("== 2. _build_cues 词数 / 字数上限切分 ==")
# 14 个词 -> 应至少切 1 次（>=14）
many = [{"start": float(i), "end": float(i) + 0.3, "word": "词"} for i in range(20)]
c = ks._build_cues(many)
check("长序列被切分(>1行)", len(c) > 1, f"得到 {len(c)} 行")

# 单元素
single = [{"start": 0.0, "end": 0.5, "word": "你好"}]
c = ks._build_cues(single)
check("单元素=1行", len(c) == 1 and c[0][2] == "你好")

# 空列表
c = ks._build_cues([])
check("空列表=0行", len(c) == 0)

# ---------------------------------------------------------------------------
print("== 3. _join_words CJK / 英文混合 ==")
check("纯中文无空格", ks._join_words(["我们", "今天", "去", "公园"]) == "我们今天去公园")
check("英文间加空格", ks._join_words(["we", "are", "good"]) == "we are good")
check("中英文交界无空格", ks._join_words(["hello", "世界"]) == "hello世界")
check("尾部空格忽略", ks._join_words(["", "  ", "我们"]) == "我们")

# ---------------------------------------------------------------------------
print("== 4. SRT / VTT 时间格式 ==")
check("SRT 毫秒逗号", ks._ts_srt(3661.500) == "01:01:01,500", ks._ts_srt(3661.5))
check("VTT 毫秒点", ks._ts_vtt(3661.500) == "01:01:01.500", ks._ts_vtt(3661.5))
check("负数钳为0", ks._ts_srt(-5) == "00:00:00,000")

# ---------------------------------------------------------------------------
print("== 5a. scene_subtitles 长停顿切分（单语音块内 >1.2s 停顿→2 行）==")
# 单段语音 0-7s，中间 2.2s→5.45s 是 3.25s 自然停顿（>1.2），应切成两行
blocks = [("speech", 0.0, 7.0)]
words = [
    {"start": 0.5, "end": 1.2, "word": "我们"},
    {"start": 1.5, "end": 2.2, "word": "今天"},
    {"start": 5.45, "end": 6.05, "word": "去"},
    {"start": 6.25, "end": 6.9, "word": "公园"},
]
srt, vtt, txt = ks.scene_subtitles(words, blocks)
check("返回非None", srt and vtt and txt is not None)
expected_lines = [l for l in srt.splitlines() if "-->" in l]
check("SRT 含 2 条时间轴", len(expected_lines) == 2, f"得到 {len(expected_lines)} 条")
if len(expected_lines) == 2:
    # 第一行 我们今天；第二行 去公园
    body = [l for l in srt.splitlines() if l not in ("1", "2") and "-->" not in l and l != ""]
    check("第一行文案=我们今天", "我们今天" in srt, f"srt={srt!r}")
    check("文案=我们今天去公园", txt == "我们今天去公园", f"txt={txt!r}")
    check("VTT 头", vtt.startswith("WEBVTT"))
    check("SRT 序号", srt.splitlines()[0] == "1")

print("== 5b. scene_subtitles 被切区间词丢弃 ==")
# 保留 0-1s 与 4-6s 两截，中间 1-4s 是切除区；'嗯' 落在切除区应丢弃
blocks = [("speech", 0.0, 1.0), ("speech", 4.0, 6.0)]
words = [
    {"start": 0.5, "end": 0.9, "word": "我们"},
    {"start": 2.5, "end": 3.0, "word": "嗯"},   # 落在切除区 -> 丢弃
    {"start": 4.2, "end": 4.8, "word": "去"},
    {"start": 5.0, "end": 5.9, "word": "公园"},
]
srt, vtt, txt = ks.scene_subtitles(words, blocks)
check("返回非None", srt is not None and vtt is not None and txt is not None)
check("文案不含被切'嗯'", "嗯" not in txt, f"txt={txt!r}")
check("文案含去公园", "去公园" in txt, f"txt={txt!r}")
check("文案含我们", "我们" in txt, f"txt={txt!r}")

# ---------------------------------------------------------------------------
print("== 6. scene_subtitles words=None ==")
srt, vtt, txt = ks.scene_subtitles(None, [("speech", 0, 1)])
check("None -> 全None", srt is None and vtt is None and txt is None)

# ---------------------------------------------------------------------------
print(f"\n结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
