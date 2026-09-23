#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Normalize skill text files to UTF-8 (no BOM).

Some editors on Windows save files as system ANSI (GBK) even when the content is
Chinese text. This helper detects such files and rewrites them as UTF-8.

Usage:
    python fix_encoding.py [extra_file ...]
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGETS = {".py", ".json", ".md", ".txt", ".yaml", ".yml"}
ENCODINGS = ("utf-8", "gbk", "gb18030")


def detect(raw):
    for enc in ENCODINGS:
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return None


def normalize(path, root):
    raw = path.read_bytes()
    enc = detect(raw)
    label = str(path)
    try:
        label = str(path.relative_to(root))
    except ValueError:
        pass
    if enc is None:
        print("UNREADABLE", label)
        return 0
    if enc == "utf-8":
        print("OK        ", label)
        return 0
    path.write_text(raw.decode(enc), encoding="utf-8", newline="\n")
    print("FIXED     ", label, "<-", enc)
    return 1


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    changed = 0
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and path.suffix.lower() in TARGETS:
            changed += normalize(path, ROOT)

    for extra in sys.argv[1:]:
        path = pathlib.Path(extra)
        if path.is_file():
            changed += normalize(path, ROOT)

    print("changed:", changed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
