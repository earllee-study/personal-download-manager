# -*- mode: python ; coding: utf-8 -*-
"""Native messaging host (stdio, no GUI, no Qt)."""

import os

HERE = os.path.dirname(os.path.abspath(SPEC))
ICON = os.path.join(HERE, "icon.ico")

a = Analysis(
    [os.path.join(HERE, "entry_host.py")],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "PySide6", "httpx", "h2", "socksio"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pdm-host",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=ICON if os.path.exists(ICON) else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="pdm-host",
)
