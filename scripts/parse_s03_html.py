import json
import re
from pathlib import Path

h = Path("data/debug/s03_user_info_before_fill_20260707_092602.html").read_text(encoding="utf-8")
print("html length:", len(h))

inputs = re.findall(r'<input[^>]+>', h, re.I)
selects = re.findall(r'<select[^>]*>.*?</select>', h, re.S | re.I)
labels = re.findall(r'<label[^>]*for="([^"]+)"[^>]*>([^<]*)', h, re.I)
ids = re.findall(r'\bid="([a-zA-Z][^"]*)"', h)
print("input count", len(inputs))
print("select count", len(selects))
print("label count", len(labels))

for inp in inputs[:40]:
    if "hidden" in inp.lower():
        continue
    print(inp[:200])

print("\n--- selects ---")
for s in selects[:10]:
    print(s[:400].replace("\n", " "))

print("\n--- labels ---")
for fid, txt in labels[:30]:
    print(fid, txt.strip()[:60])

# rootData snippet
m = re.search(r'var rootData = (\{.*?\});', h, re.S)
if m:
    try:
        data = json.loads(m.group(1))
        print("\nrootData keys:", list(data.keys())[:20])
    except Exception as e:
        print("rootData parse err", e)
