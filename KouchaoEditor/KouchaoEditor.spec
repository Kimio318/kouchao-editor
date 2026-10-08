# -*- mode: python ; coding: utf-8 -*-
# Copyright (c) 2026 Kimio318
# SPDX-License-Identifier: MIT
# KouchaoEditor（口播剪辑器）—— 自有源码，采用 MIT 许可证（详见 LICENSE）。

import os

# 仓库根（KouchaoEditor/ 的上一级），用于把许可证文件打进发布包
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


a = Analysis(
    ['src/kouchao_server.py'],
    pathex=[],
    binaries=[],
    datas=[
        (os.path.join(_ROOT, "LICENSE"), "."),
        (os.path.join(_ROOT, "THIRD-PARTY-NOTICES.md"), "."),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='KouchaoEditor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
