import urllib.request, json, sys

BASE = "http://127.0.0.1:8021"
VID = r"E:\口播测试\260812.mp4"

def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=1800)

print("== analyze (silence only) ==")
j = json.load(post("/api/analyze", {
    "path": VID, "thr": -28, "min": 0.4, "pad": 0.1, "minSeg": 0.3, "filler": False}))
print("origDur=%.2f  segments=%d  silences=%d" % (j["origDur"], len(j["segments"]), j["silences"]))

print("== cut ==")
data = post("/api/cut", {
    "path": VID, "segments": j["segments"], "filler": False}).read()
with open("out_test.mp4", "wb") as f:
    f.write(data)
print("cut output bytes =", len(data))

# 验证输出是合法 mp4
import subprocess
ffprobe = r"C:\Users\admin\WorkBuddy\2026-08-18-14-57-43\KouchaoEditor\ffmpeg\ffprobe.exe"
out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", "out_test.mp4"],
                    capture_output=True, text=True)
print("out_test.mp4 duration =", out.stdout.strip())
print("E2E OK" if len(data) > 1000 and out.stdout.strip() else "E2E FAIL")
