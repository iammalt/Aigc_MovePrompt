#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""构建 drama-prompt 数据集：用标准库 zipfile 解析两个 xlsx，导出 data/params.json。

仅依赖 Python 标准库（zipfile / xml.etree / json / re），不引入 openpyxl，
保证运行时零第三方依赖。

用法:
    python build_data.py
    python build_data.py --jimeng "D:/x/即梦800个神级指令合集.xlsx" --camera "D:/x/运镜方式.xlsx"
    python build_data.py --no-filter      # 关闭内容安全过滤

产物 data/params.json 结构:
    {
      "dimensions": [ {key, label, count, items, replaced_by_subject?} x7 ],
      "cameras":    [ {"name": 中文名称, "desc": 详细解释}, ... ]
    }

清洗内容:
    - 去除 UTF-8 BOM 字符 \\ufeff（表格中位置随机、可能重复出现）
    - 去除首尾空格、全角空格，内部换行/连续空白压缩为单个空格
    - 跳过空行与空单元格
    - 同一维度内去重并保持原有顺序
    - 丢弃命中内容安全词表的条目（构建时打印明细）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = SKILL_DIR / "data"

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# key 与 Excel 列名的对应关系；character 维度由用户输入的主体替代，不参与随机
DIMENSIONS = [
    ("style", "风格与艺术表现"),
    ("lens", "镜头与构图"),
    ("character", "人物与服饰"),
    ("scene", "场景与背景"),
    ("detail", "细节与服饰"),
    ("tone", "色调与光效"),
    ("culture", "文化与环境"),
]

BOM = "\ufeff"

# 内容安全过滤：命中任一关键词的条目会被丢弃，避免产出被平台审核拦截的提示词
DEFAULT_FILTER = ("妓院", "裸体", "情色", "色情", "强奸", "幼女")

# 运镜表优先取「详细解释」作为运镜说明，缺失时回退到下列列
CAMERA_DESC_FALLBACK = ["详细解释", "示例提示词中文翻译", "英文名称", "示例提示词"]


def clean(value: object) -> str:
    """清洗单个单元格文本。"""
    if value is None:
        return ""
    text = str(value).replace(BOM, "")
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n，,、；;|")


def col_to_idx(col: str) -> int:
    """列字母转 1-based 序号：A=1, B=2, ..., Z=26, AA=27。"""
    idx = 0
    for ch in col:
        idx = idx * 26 + (ord(ch) - 64)
    return idx


def read_shared_strings(zf: zipfile.ZipFile) -> list:
    names = [n for n in zf.namelist() if n.endswith("sharedStrings.xml")]
    if not names:
        return []
    root = ET.fromstring(zf.read(names[0]))
    strings = []
    for si in root.findall(f"{NS}si"):
        texts = [t.text or "" for t in si.iter(f"{NS}t")]
        strings.append("".join(texts))
    return strings


def read_sheet_rows(zf: zipfile.ZipFile, sheet_path: str, shared: list) -> list:
    """读取一个 worksheet，返回 list[dict[列序号 -> 单元格原始文本]]。"""
    root = ET.fromstring(zf.read(sheet_path))
    data = root.find(f"{NS}sheetData")
    if data is None:
        return []
    rows = []
    for row in data.findall(f"{NS}row"):
        cells = {}
        for c in row.findall(f"{NS}c"):
            ref = c.get("r") or ""
            m = re.match(r"([A-Z]+)(\d+)", ref)
            if not m:
                continue
            col = col_to_idx(m.group(1))
            t = c.get("t")
            v = c.find(f"{NS}v")
            isn = c.find(f"{NS}is")
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif isn is not None:
                val = "".join(tt.text or "" for tt in isn.iter(f"{NS}t"))
            elif v is not None:
                val = v.text
            else:
                val = ""
            cells[col] = val
        rows.append(cells)
    return rows


def load_rows_by_marker(path: Path, marker: str):
    """打开 xlsx，返回第一个含 marker 表头的工作表行列表（list[dict[列序号->文本]]）。"""
    with zipfile.ZipFile(path) as zf:
        shared = read_shared_strings(zf)
        sheet_paths = sorted(n for n in zf.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        for sp in sheet_paths:
            rows = read_sheet_rows(zf, sp, shared)
            for idx, row in enumerate(rows):
                cleaned = {c: clean(val) for c, val in row.items()}
                if marker in cleaned.values():
                    return rows
    raise RuntimeError(f"未能在 {path.name} 中定位表头（缺少列名 {marker!r}），请检查 Excel 结构")


def find_header(rows, marker: str):
    """定位包含 marker 的表头行，返回 (行号, 清洗后的整行 dict[列序号->文本])。"""
    for idx, row in enumerate(rows):
        cleaned = {c: clean(val) for c, val in row.items()}
        if marker in cleaned.values():
            return idx, cleaned
    raise RuntimeError(f"未能在表格中定位表头（缺少列名 {marker!r}）")


def dedupe(items):
    return list(dict.fromkeys(items))


def load_jimeng(path: Path, blocklist=DEFAULT_FILTER):
    rows = load_rows_by_marker(path, "风格与艺术表现")
    header_idx, header = find_header(rows, "风格与艺术表现")

    col_of = {}
    for key, label in DIMENSIONS:
        cols = [c for c, v in header.items() if v == label]
        if not cols:
            raise RuntimeError(f"表头缺少列 {label!r}")
        col_of[key] = cols[0]

    pools = {key: [] for key in col_of}
    dropped = {}
    for row in rows[header_idx + 1:]:
        for key, col in col_of.items():
            value = clean(row.get(col, ""))
            if not value:
                continue
            hit = next((term for term in blocklist if term in value), None)
            if hit:
                dropped.setdefault(hit, []).append(value)
                continue
            pools[key].append(value)

    dimensions = []
    for key, label in DIMENSIONS:
        items = dedupe(pools[key])
        entry = {"key": key, "label": label, "count": len(items), "items": items}
        if key == "character":
            entry["replaced_by_subject"] = True
        dimensions.append(entry)

    return {
        "source": path.name,
        "column_order": [label for _, label in DIMENSIONS],
        "note": "character（人物与服饰）由用户输入的主体替代，不参与随机抽取。",
        "dimensions": dimensions,
    }, dropped


def load_camera(path: Path, blocklist=DEFAULT_FILTER):
    rows = load_rows_by_marker(path, "中文名称")
    header_idx, header = find_header(rows, "中文名称")

    name_col = next(c for c, v in header.items() if v == "中文名称")
    desc_col = None
    for cand in CAMERA_DESC_FALLBACK:
        cols = [c for c, v in header.items() if v == cand]
        if cols:
            desc_col = cols[0]
            break

    cameras = []
    seen = set()
    for row in rows[header_idx + 1:]:
        name = clean(row.get(name_col, ""))
        if not name:
            continue
        if any(term in name for term in blocklist):
            continue
        desc = clean(row.get(desc_col, "")) if desc_col else ""
        if name in seen:
            continue
        seen.add(name)
        cameras.append({"name": name, "desc": desc})

    return {"source": path.name, "count": len(cameras), "cameras": cameras}


def main() -> int:
    parser = argparse.ArgumentParser(description="解析 Excel 参数表并导出 drama-prompt 数据集 JSON")
    parser.add_argument("--jimeng", default=str(DEFAULT_DATA_DIR / "即梦800个神级指令合集.xlsx"), help="即梦800个神级指令合集.xlsx 路径")
    parser.add_argument("--camera", default=str(DEFAULT_DATA_DIR / "运镜方式.xlsx"), help="运镜方式.xlsx 路径")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="JSON 输出目录")
    parser.add_argument("--no-filter", action="store_true", help="关闭内容安全词过滤（默认开启）")
    args = parser.parse_args()

    jimeng_path = Path(args.jimeng)
    camera_path = Path(args.camera)
    for path in (jimeng_path, camera_path):
        if not path.exists():
            print(f"[错误] 找不到文件: {path}", file=sys.stderr)
            return 1

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    blocklist = () if args.no_filter else DEFAULT_FILTER
    jimeng, dropped = load_jimeng(jimeng_path, blocklist)
    camera = load_camera(camera_path, blocklist)

    params = {
        "source": {"jimeng": jimeng["source"], "camera": camera["source"]},
        "dimensions": jimeng["dimensions"],
        "cameras": camera["cameras"],
    }

    (data_dir / "params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if dropped:
        print("[过滤] 命中内容安全词表，已丢弃以下条目：")
        for term, values in dropped.items():
            for value in values:
                print(f"  - {value}  (命中: {term})")

    print(f"[完成] 输出: {data_dir / 'params.json'}")
    for dim in jimeng["dimensions"]:
        flag = "  (由主体替代)" if dim.get("replaced_by_subject") else ""
        print(f"  - {dim['label']}: {dim['count']} 条{flag}")
    print(f"  - 运镜: {camera['count']} 条 (每图必含 1 条)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
