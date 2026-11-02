#!/usr/bin/env python3
"""文档 lint：BOM / 末尾换行 / 死链 / 无语言围栏 / 内部字段泄漏"""
import os, re, glob, sys
bad = 0
def err(m):
    global bad; bad += 1; print("FAIL:", m)

# 1. BOM + 末尾换行
for p in ["README.md"] + sorted(glob.glob("0[1-9]-*/README.md")):
    try:
        b = open(p, "rb").read()
    except FileNotFoundError:
        continue
    if b.startswith(b"\xef\xbb\xbf"): err(f"{p}: UTF-8 BOM")
    if b and not b.endswith(b"\n"):   err(f"{p}: no trailing newline")

# 2. 死链 + 无语言围栏 + 内部字段
pat = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
for p in glob.glob("**/*.md", recursive=True):
    if p.startswith(".git/"): continue
    txt = open(p, encoding="utf-8-sig").read()
    for text, url in pat.findall(txt):
        if url.startswith(("http", "mailto", "#")): continue
        tgt = os.path.normpath(os.path.join(os.path.dirname(p), url.split("#")[0]))
        if url.split("#")[0] and not os.path.exists(tgt): err(f"{p}: dead link -> {url}")
    if '层归属' in txt or '前置资产' in txt:
        err(f"{p}: internal company fields leaked")
    in_f = False
    for i, l in enumerate(txt.split("\n"), 1):
        if l.strip().startswith("```"):
            if not in_f and not l.strip()[3:].strip(): err(f"{p}:{i}: fence without language")
            in_f = not in_f

print(f"\n{'OK: 0 issues' if bad==0 else f'FAIL: {bad} issues'}")
sys.exit(1 if bad else 0)
