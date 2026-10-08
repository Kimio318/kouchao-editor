#!/usr/bin/env python3
# 端到端验证：用构建好的便携 exe 跑 4 种场景，检查产出合法且符合预期
import urllib.request, json, os, subprocess, zipfile, sys

BASE = r"C:\Users\admin\WorkBuddy\2026-08-18-14-57-43\KouchaoEditor"
FFPROBE = os.path.join(BASE, "ffmpeg", "ffprobe.exe")
VIDEO = r"E:\口播测试\260812.mp4"
URL = "http://127.0.0.1:8021"

def dur(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                          "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
                         capture_output=True, text=True, timeout=60)
    return float(out.stdout.strip() or 0)

def post(path, payload, binary=False):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(URL + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        ctype = r.headers.get("Content-Type", "")
        body = r.read()
        return ctype, body

def analyze(payload):
    ctype, body = post("/api/analyze", payload)
    return json.loads(body)

def cut(payload, out_path):
    ctype, body = post("/api/cut", payload)
    with open(out_path, "wb") as f:
        f.write(body)
    return ctype, body

print("视频原时长: %.2fs" % dur(VIDEO))

# 1) 切除模式（无语气词）
print("\n=== 场景1：切除模式 ===")
a = analyze({"path": VIDEO, "thr": -28, "min": 0.4, "pad": 0.1, "minSeg": 0.3,
             "filler": False, "pauseMode": "cut"})
print("保留段数=%d 场景数=%d" % (len(a["segments"]), a.get("sceneCount", 1)))
ct, _ = cut({"path": VIDEO, "thr": -28, "min": 0.4, "pad": 0.1, "minSeg": 0.3,
             "filler": False, "pauseMode": "cut"}, "_t1_cut.mp4")
print("产出类型=%s 时长=%.2fs" % (ct, dur("_t1_cut.mp4")))

# 2) 压缩模式
print("\n=== 场景2：压缩模式（保留喘息）===")
a = analyze({"path": VIDEO, "thr": -28, "min": 0.4, "pad": 0.1, "minSeg": 0.3,
             "filler": False, "pauseMode": "compress", "floorGap": 0.25})
print("保留段数=%d 场景数=%d" % (len(a["segments"]), a.get("sceneCount", 1)))
ct, _ = cut({"path": VIDEO, "thr": -28, "min": 0.4, "pad": 0.1, "minSeg": 0.3,
             "filler": False, "pauseMode": "compress", "floorGap": 0.25}, "_t2_compress.mp4")
d2 = dur("_t2_compress.mp4")
print("产出类型=%s 时长=%.2fs" % (ct, d2))

# 3) 两级语气词
print("\n=== 场景3：两级语气词切除 ===")
a = analyze({"path": VIDEO, "thr": -28, "min": 0.4, "pad": 0.1, "minSeg": 0.3,
             "filler": True, "lang": "zh",
             "fillerWords": "um,uh,er,ah,呃,额",
             "fillerWords2": "然后,就是,嗯,啊,那个,那,对,所以,但是",
             "pauseMode": "cut"})
print("保留段数=%d 语气词命中=%d 场景数=%d" % (len(a["segments"]), a.get("fillerCount", 0), a.get("sceneCount", 1)))
if a.get("fillers"):
    print("前几个语气词：", [(f["word"], f["start"], f["end"]) for f in a["fillers"][:8]])
ct, _ = cut({"path": VIDEO, "thr": -28, "min": 0.4, "pad": 0.1, "minSeg": 0.3,
             "filler": True, "lang": "zh",
             "fillerWords": "um,uh,er,ah,呃,额",
             "fillerWords2": "然后,就是,嗯,啊,那个,那,对,所以,但是",
             "pauseMode": "cut"}, "_t3_filler.mp4")
print("产出类型=%s 时长=%.2fs" % (ct, dur("_t3_filler.mp4")))

# 4) 自然切片
print("\n=== 场景4：自然切片 ===")
a = analyze({"path": VIDEO, "thr": -28, "min": 0.4, "pad": 0.1, "minSeg": 0.3,
             "filler": False, "pauseMode": "cut", "sliceMode": True, "sliceThreshold": 1.5})
print("保留段数=%d 场景数=%d" % (len(a["segments"]), a.get("sceneCount", 1)))
ct, body = cut({"path": VIDEO, "thr": -28, "min": 0.4, "pad": 0.1, "minSeg": 0.3,
                "filler": False, "pauseMode": "cut", "sliceMode": True, "sliceThreshold": 1.5},
               "_t4_slice.zip")
if "zip" in ct:
    with zipfile.ZipFile("_t4_slice.zip") as z:
        names = z.namelist()
    print("产出 zip，含 %d 个片段：%s" % (len(names), names))
    zf = zipfile.ZipFile("_t4_slice.zip")
    zf.extractall("_t4_out")
    for n in sorted(os.listdir("_t4_out")):
        print("  -", n, "%.2fs" % dur(os.path.join("_t4_out", n)))
else:
    print("!!! 期望 zip，实际得到", ct)

print("\n全部场景完成。")
