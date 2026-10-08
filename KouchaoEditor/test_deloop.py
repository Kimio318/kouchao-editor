# -*- coding: utf-8 -*-
"""复读清理 / 模型能力探测 单元测试。
测试数据直接用用户实际遇到的粤语识别崩溃文本。
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
import kouchao_server as ks

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# ============ 用户真实的崩溃文本 ============
REAL = """延載我發覺很多人很努力,他們為甚麼原努力,就變得抗
因為延載很多人,社會已經成熟了,各項國已經是把握了
就是說任何進入,有任何一個採讀,都是很多記憶
因為這一個全世界有十億人離走,有幾億人都想做老闆
你想到的東西,人家又想通,所以延載很多人很努力,不是他想得贏得
所以延載你所採讀的成功,不需要重新學習,所以延載家人才是最重要的,白癡九個九個人
所以延載你所採讀的成功,是很努力的,所以延載你所採讀的成功,很努力的,長載你所採讀的,長載你所採讀的,長載你所採讀的,長載你所採讀的,長載你所採讀的。
所以延載你所採讀的,長載你所採讀的,長載你所採讀的,長載你所採讀的,長載你所採讀的,長載你所採讀的,長載你所採讀的,長載你所採讀的,長載你所採讀的。"""

print("== 1. 字符级去复读 _collapse_char_loop ==")
check("AAAA -> A", ks._collapse_char_loop("啊啊啊啊") == "啊", repr(ks._collapse_char_loop("啊啊啊啊")))
SRC = "長載你所採讀的,長載你所採讀的,長載你所採讀的,長載你所採讀的"
r = ks._collapse_char_loop(SRC)
# 带逗号的 3 个完整单元被坍缩为 1 个，末尾那个不带逗号的是「残尾」，
# 字符级按 min_reps=3 管不到它 —— 残尾由短句级 _dedup_clauses 负责，二者组合才彻底干净。
check("字符级把3个完整单元坍缩掉", r.count("長載你所採讀的") == 2, repr(r))
check("字符级确实缩短了文本", len(r) < len(SRC))
c, _ = ks._clean_transcript(SRC)
check("组合清理后只剩一次", c.count("長載你所採讀的") == 1, repr(c))
check("正常文本不被破坏", ks._collapse_char_loop("我今天去公園散步") == "我今天去公園散步")
check("两次重复不动（min_reps=3）", ks._collapse_char_loop("好的好的") == "好的好的")
check("空串安全", ks._collapse_char_loop("") == "")

print("\n== 2. 短句级去复读 _dedup_clauses ==")
r = ks._dedup_clauses("長載你所採讀的,長載你所採讀的,長載你所採讀的。")
check("A,A,A -> A", r.count("長載你所採讀的") == 1, repr(r))
r = ks._dedup_clauses("甲,乙,甲,乙,甲,乙。")
check("A,B,A,B 交替复读被清理", r.count("甲") == 1 and r.count("乙") == 1, repr(r))
r = ks._dedup_clauses("我今天去公園,天氣很好,然後回家。")
check("正常句子全保留", r == "我今天去公園,天氣很好,然後回家。", repr(r))

print("\n== 3. 端到端清理真实崩溃文本 ==")
cleaned, ratio = ks._clean_transcript(REAL)
print("---- 清理后 ----")
print(cleaned)
print("---- 复读率 =", ratio, "----")
n_before = REAL.count("長載你所採讀的")
n_after = cleaned.count("長載你所採讀的")
check(f"复读句大幅减少（{n_before} -> {n_after}）", n_after < 3, f"仍有 {n_after} 次")
check("复读率被检测出来（>0.3）", ratio > 0.3, f"ratio={ratio}")
check("正常内容保留：全世界有十億人", "全世界有十億人" in cleaned)
check("正常内容保留：社會已經成熟了", "社會已經成熟了" in cleaned)
check("第一行完整保留", "延載我發覺很多人很努力" in cleaned)
check("行数不增加", len(cleaned.splitlines()) <= len(REAL.splitlines()))

print("\n== 4. 词级去复读 _dedup_words ==")
w = [{"word": "長載", "start": 0.0, "end": 0.2}, {"word": "你所", "start": 0.2, "end": 0.4}]
loop = []
for k in range(6):
    loop.append({"word": "長載", "start": k * 0.4, "end": k * 0.4 + 0.2})
    loop.append({"word": "你所", "start": k * 0.4 + 0.2, "end": k * 0.4 + 0.4})
out, dropped = ks._dedup_words(loop)
check("6次2词复读 -> 保留1次", len(out) == 2, f"len={len(out)}")
check("丢弃计数正确", dropped == 10, f"dropped={dropped}")
check("保留的是首次时间戳", out[0]["start"] == 0.0, f"{out[0]}")

norm = [{"word": w_, "start": i * 0.3, "end": i * 0.3 + 0.25}
        for i, w_ in enumerate(["我们", "今天", "去", "公园", "散步"])]
out2, d2 = ks._dedup_words(norm)
check("正常词表不被误删", len(out2) == 5 and d2 == 0, f"len={len(out2)} dropped={d2}")

single = [{"word": "嗯", "start": 0, "end": 0.2}]
out3, d3 = ks._dedup_words(single)
check("单词表安全", len(out3) == 1 and d3 == 0)
out4, d4 = ks._dedup_words([])
check("空词表安全", out4 == [] and d4 == 0)

print("\n== 5. 模型档位与粤语能力探测 ==")
check("base 档识别", ks._model_tier("x/ggml-base.bin") == (30, "base"), str(ks._model_tier("x/ggml-base.bin")))
check("large-v3-turbo 优先级最高",
      ks._model_tier("ggml-large-v3-turbo.bin")[0] > ks._model_tier("ggml-large-v3.bin")[0])
check("large-v3 > medium", ks._model_tier("ggml-large-v3.bin")[0] > ks._model_tier("ggml-medium.bin")[0])
check("medium > small", ks._model_tier("ggml-medium.bin")[0] > ks._model_tier("ggml-small.bin")[0])
check("small > base", ks._model_tier("ggml-small.bin")[0] > ks._model_tier("ggml-base.bin")[0])
check(".en 单语模型被压到最低", ks._model_tier("ggml-medium.en.bin")[0] == 1,
      str(ks._model_tier("ggml-medium.en.bin")))
check("base 被判为对CJK偏弱", ks._model_tier("ggml-base.bin")[0] <= 50)

m = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whisper", "models", "ggml-base.bin")
if os.path.isfile(m):
    meta = ks._read_ggml_meta(m)
    check("读出 n_vocab=51865", meta.get("n_vocab") == 51865, str(meta))
    check("判定为多语言", meta.get("multilingual") is True)
    check("判定为不支持粤语", meta.get("hasYue") is False, str(meta.get("hasYue")))
    mi = ks.model_info(m)
    check("model_info 档位=base", mi.get("tier") == "base", str(mi))
    check("model_info 标记 weakForCJK", mi.get("weakForCJK") is True)
    check("model_info hasYue=False", mi.get("hasYue") is False)
else:
    print("  (跳过真实模型探测：未找到 ggml-base.bin)")

check("坏文件返回空 meta", ks._read_ggml_meta(__file__) == {})
check("不存在的文件安全", ks._read_ggml_meta("nope_not_exist.bin") == {})

print(f"\n===== 通过 {PASS} 项，失败 {FAIL} 项 =====")
sys.exit(1 if FAIL else 0)
