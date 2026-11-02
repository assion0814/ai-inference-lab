#!/usr/bin/env python3
"""Patch Ray's version check to allow Python patch-version mismatch."""
import re
import sys

p = "/root/.local/lib/python3.10/site-packages/ray/_private/utils.py"
src = open(p).read()

# 检查是否已 patch
if "[PATCHED-NOOP]" in src:
    print("already patched")
    sys.exit(0)

# 把 "if raise_on_mismatch:\n                raise RuntimeError(error_message)"
# 改成不 raise，只 warning
old = """        if raise_on_mismatch:
            raise RuntimeError(error_message)
        else:
            logger.warning(error_message)"""
new = """        # [PATCHED-NOOP] heterogeneous cluster: warn instead of raising
        logger.warning(error_message)"""
if old in src:
    src = src.replace(old, new)
    open(p, "w").write(src)
    print("patched: raise -> warning")
else:
    # 备用：找 raise RuntimeError(error_message) 在版本检查上下文中
    # 限制替换范围
    idx = src.find('def check_version_info')
    end = src.find('\ndef ', idx + 1)
    if idx < 0 or end < 0:
        print("cannot locate check_version_info")
        sys.exit(1)
    chunk = src[idx:end]
    if 'raise RuntimeError(error_message)' in chunk:
        new_chunk = chunk.replace(
            'if raise_on_mismatch:\n            raise RuntimeError(error_message)\n        else:\n            logger.warning(error_message)',
            '# [PATCHED-NOOP] warn only\n        logger.warning(error_message)'
        )
        if new_chunk == chunk:
            # 更宽松
            new_chunk = chunk.replace('raise RuntimeError(error_message)', 'logger.warning(error_message)')
        src = src[:idx] + new_chunk + src[end:]
        open(p, "w").write(src)
        print("patched (fallback)")
    else:
        print("raise not found in function")
        sys.exit(1)
