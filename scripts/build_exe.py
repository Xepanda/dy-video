# -*- coding: utf-8 -*-
"""
scripts/build_exe.py - 构建 Windows 独立可执行桌面应用 (.exe)
============================================================
将 src/tools/desktop_spec_creator.py 打包为独立的 Windows 可执行文件，
输出到项目根目录下，方便用户直接双击运行。
"""

import os
import sys
import shutil
import subprocess

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_exe():
    entry_point = os.path.join(PROJECT_ROOT, "src", "tools", "desktop_spec_creator.py")
    dist_dir = os.path.join(PROJECT_ROOT, "dist")
    build_dir = os.path.join(PROJECT_ROOT, "build")
    internal_name = "SpecCreator"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",  # 无黑框控制台
        "--name", internal_name,
        "--collect-all", "webview",
        "--hidden-import", "clr",
        "--hidden-import", "pythonnet",
        "--hidden-import", "webview.platforms.winforms",
        "--hidden-import", "webview.platforms.edgechromium",
        "--distpath", dist_dir,
        "--workpath", build_dir,
        entry_point
    ]

    print("=" * 60)
    print("开始打包桌面任务配置器 EXE...")
    print(f"入口文件: {entry_point}")
    print(f"执行命令: {' '.join(cmd)}")
    print("=" * 60)

    res = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if res.returncode != 0:
        print("[ERROR] PyInstaller 打包失败！")
        return False

    built_exe = os.path.join(dist_dir, f"{internal_name}.exe")
    target_cn_exe = os.path.join(PROJECT_ROOT, "启动任务配置器.exe")
    target_en_exe = os.path.join(PROJECT_ROOT, "SpecCreator.exe")

    if os.path.exists(built_exe):
        shutil.copy2(built_exe, target_cn_exe)
        shutil.copy2(built_exe, target_en_exe)
        print("=" * 60)
        print(f"[SUCCESS] 打包成功！产物已复制到项目根目录:")
        print(f"  --> {target_cn_exe}")
        print(f"  --> {target_en_exe}")
        print("=" * 60)
        return True
    else:
        print(f"[ERROR] 未找到生成的 EXE 文件: {built_exe}")
        return False


if __name__ == "__main__":
    success = build_exe()
    sys.exit(0 if success else 1)
