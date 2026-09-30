# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

datas = []
if Path('.env.example').exists():
    datas.append(('.env.example', '.'))

binaries = []
hiddenimports = [
    'telethon',
    'telethon.tl',
    'telethon.tl.alltlobjects',
    'telethon.extensions',
    'telethon.crypto',
    'tracker',
    'engine',
    'customtkinter',
    'PIL',
    'sqlite3',
    'dotenv',
]

ctk_data = collect_all('customtkinter')
datas += ctk_data[0]
binaries += ctk_data[1]
hiddenimports += ctk_data[2]

a = Analysis(
    ['gui.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='Saveit',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
