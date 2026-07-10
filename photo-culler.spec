# -*- mode: python ; coding: utf-8 -*-
import os
import re
import sys
from pathlib import Path

# CI sets APP_VERSION from the git tag (e.g. v0.1.0); local builds get 0.0.0.
APP_VERSION = os.environ.get("APP_VERSION", "v0.0.0").lstrip("v")

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

if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    # File-properties version resource needs four numeric parts; keep the full
    # tag (incl. -rcN suffixes) in the string fields.
    nums = ([int(x) for x in re.findall(r"\d+", APP_VERSION)] + [0, 0, 0, 0])[:4]
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=tuple(nums), prodvers=tuple(nums)),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("ProductName", "PhotoCuller"),
                            StringStruct("FileDescription", "PhotoCuller - photo culling app"),
                            StringStruct("ProductVersion", APP_VERSION),
                            StringStruct("FileVersion", APP_VERSION),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )

    # Portable single-file exe: everything self-extracts to temp at launch.
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        name="PhotoCuller",
        console=False,
        icon="assets/icon.ico",
        version=version_info,
    )
else:
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
                "CFBundleShortVersionString": APP_VERSION.split("-")[0],
                "CFBundleVersion": APP_VERSION,
                "NSHighResolutionCapable": True,
                "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
            },
        )
