#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""临时诊断：统计词条的主题簇标签覆盖率，用于调优 consistency.json。"""

import json
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DATA = Path(__file__).resolve().parent.parent / "data"
data = json.loads((DATA / "jimeng_prompts.json").read_text(encoding="utf-8"))
rules = json.loads((DATA / "consistency.json").read_text(encoding="utf-8"))
themes = rules["themes"]


def tag(text):
    return [name for name, kws in themes.items() if any(kw in text for kw in kws)]


for dim in data["dimensions"]:
    counter = Counter()
    neutral = []
    for item in dim["items"]:
        hits = tag(item)
        if hits:
            for name in hits:
                counter[name] += 1
        else:
            neutral.append(item)
    print(f"--- {dim['label']} ({dim['count']}) ---")
    print("  标签分布:", dict(counter))
    print(f"  中性 {len(neutral)} 条:", " / ".join(neutral))
