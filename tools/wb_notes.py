import re
import sys
import pathlib

s = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
m = re.search(r"^## 1\.1\.0[^\n]*\n(.*?)(?=^## |\Z)", s, re.M | re.S)
first = s.splitlines()[0] if s else ""
print("1.1.0 —— 按上游 review 修正。\n")
print(m.group(1).strip() if m else "详见 CHANGELOG.md")
