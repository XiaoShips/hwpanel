#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 PyInstaller --onedir 产物打成「根目录即 TempPeek.exe」的便携 zip。

为什么需要这个脚本
------------------
手工打的便携包把 dist/TempPeek/ 整个目录压进去了，导致 zip 内部
套了一层 `TempPeek/` 子文件夹：解压后是 `TempPeek/TempPeek.exe`，
用户还要再点进一层才能运行。这里改成把 `dist/TempPeek` 的**内容**
（TempPeek.exe + _internal/...）直接压到 zip 根，解压即见 exe，
双击即可运行，无需进入子文件夹。

自更新兼容性
------------
tempeek.py 的 extract_package 用 os.walk 递归查找 TempPeek.exe，
因此无论 exe 在 zip 根还是套一层都能定位，换布局不会打断自动更新。

用法
----
    python packaging/zip_portable.py
版本号与输出文件名自动从仓库根 version.json 读取。
"""
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根
SRC = os.path.join(ROOT, "dist", "TempPeek")          # PyInstaller onedir 产物
RELEASE = os.path.join(ROOT, "release")


def main():
    if not os.path.isdir(SRC):
        sys.exit("找不到 %s\n请先运行 packaging/build.bat 生成 onedir 产物。" % SRC)

    ver_path = os.path.join(ROOT, "version.json")
    if not os.path.isfile(ver_path):
        sys.exit("找不到 version.json")
    with open(ver_path, encoding="utf-8") as fh:
        ver = str(json.load(fh).get("version", "")).strip().lstrip("vV")
    if not ver:
        sys.exit("version.json 缺少 version 字段")

    os.makedirs(RELEASE, exist_ok=True)
    out = os.path.join(RELEASE, "TempPeek-v%s-portable.zip" % ver)

    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, _dirs, files in os.walk(SRC):
            for name in files:
                full = os.path.join(base, name)
                # 相对 dist/TempPeek 的路径，作为 zip 内条目（统一用 /）
                arc = os.path.relpath(full, SRC).replace("\\", "/")
                zf.write(full, arc)
                count += 1

    # 验证：根目录第一项就应该是 TempPeek.exe
    with zipfile.ZipFile(out) as zf:
        top = [n for n in zf.namelist() if "/" not in n]
    print("已生成 %s" % out)
    print("  文件数: %d" % count)
    print("  根目录条目: %s" % (", ".join(top[:5]) + (" ..." if len(top) > 5 else "")))
    if "TempPeek.exe" not in top:
        sys.exit("打包异常：根目录未找到 TempPeek.exe")
    print("  OK：解压后根目录即 TempPeek.exe，无需进入子文件夹。")


if __name__ == "__main__":
    main()
