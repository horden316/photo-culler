# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

# CI downloads exiftool into vendor/exiftool/ before building; local builds
# without it fall back to a system-installed exiftool at runtime.
datas = [("web", "web")]
if Path("vendor/exiftool").exists():
    datas.append(("vendor/exiftool", "exiftool"))

a = Analysis(
    ["app/desktop.py"],
    pathex=["app"],
    datas=datas,
    hiddenimports=["pillow_heif"],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="PhotoCuller",
    console=False,
    icon="assets/icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="PhotoCuller",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="PhotoCuller.app",
        icon="assets/icon.icns",
        bundle_identifier="com.horden.photoculler",
        info_plist={
            "NSHighResolutionCapable": True,
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
