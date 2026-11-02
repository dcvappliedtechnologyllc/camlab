# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the camlab Mac app (one-dir, wrapped as camlab.app).

Build from the repo root:  .venv/bin/pyinstaller --noconfirm app/camlab.spec
(app/build.sh does that plus the post-build steps.)

Layout of the result (PyInstaller 6 .app convention):
  camlab.app/Contents/MacOS/camlab                 bootloader -> app/launcher.py
  camlab.app/Contents/Frameworks/                  == sys._MEIPASS: python runtime, site-packages (.so),
                                                   EDSDK.framework (Canon's own signature kept, see build.sh)
  camlab.app/Contents/Resources/                   data files (ui/, assets/, config.toml, recipes.json, ...);
                                                   symlinked into Frameworks so ROOT-relative paths resolve
Writable state never lives in the bundle: server/config.py puts it in
~/Library/Application Support/camlab when sys.frozen (seeded from these defaults on first launch).
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

REPO = Path(SPECPATH).resolve().parent   # app/ -> repo root
os.chdir(REPO)

# ---- data: read-only defaults, all relative to the bundle's _internal dir (== config.ROOT when frozen)
datas = [
    (str(REPO / "ui"), "ui"),
    (str(REPO / "assets"), "assets"),
    (str(REPO / "config.toml"), "."),
]
for opt in ("recipes.json", "GodoxLight.db", "GodoxLight.db.state.json", "godox_mesh.json"):
    if (REPO / opt).exists():
        datas.append((str(REPO / opt), "."))

binaries = []
hiddenimports = [
    # our own dynamic-ish bits
    *collect_submodules("server"),
    # web stack
    *collect_submodules("uvicorn"),
    *collect_submodules("websockets"),
    "uvicorn.loops.auto", "uvicorn.loops.asyncio", "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.protocols.websockets.websockets_impl", "uvicorn.lifespan.on",
    "h11", "anyio", "anyio._backends._asyncio", "sniffio", "starlette", "multipart", "email.mime.multipart",
    *collect_submodules("fastmcp"),
    *collect_submodules("mcp"),
    *collect_submodules("pydantic"),
    "pydantic_core", "pydantic_settings", "typing_extensions", "annotated_types",
    # bluetooth (Godox mesh) + Cocoa
    *collect_submodules("bleak"),
    "CoreBluetooth", "Foundation", "AppKit", "WebKit", "objc", "libdispatch", "Quartz",
    "PyObjCTools", "PyObjCTools.AppHelper",
    # pywebview cocoa backend
    "webview.platforms.cocoa", "bottle", "proxy_tools",
    # media / serial
    "serial", "serial.tools.list_ports", "PIL", "PIL.Image", "cv2", "numpy", "requests",
]

# python-gphoto2 wheel carries libgphoto2 + camlibs/iolibs as package data
gp_datas, gp_binaries, gp_hidden = collect_all("gphoto2")
datas += gp_datas
binaries += gp_binaries
hiddenimports += gp_hidden

# fastmcp / mcp ship package data (json schemas, templates)
for pkg in ("fastmcp", "mcp", "segno", "bleak", "webview", "certifi", "anthropic", "httpx2", "jiter", "distro", "docstring_parser"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:  # noqa: BLE001  (package absent)
        pass

excludes = [
    "tests", "pytest", "_pytest", "tkinter", "matplotlib", "IPython", "jupyter", "notebook",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "qtpy", "cefpython3",  # other pywebview GUIs
    "PyObjCTest",
]

a = Analysis(
    [str(REPO / "app" / "launcher.py")],
    pathex=[str(REPO)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="camlab",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # no Terminal window; logs go to ~/Library/Application Support/camlab/logs
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,  # ad-hoc
    entitlements_file=None,
)

# Canon's EDSDK.framework, whole, structure intact (Versions/A, Current symlink, bundles, _CodeSignature).
# As a Tree it is DATA for PyInstaller: nothing is stripped, relinked or re-signed by Analysis. It ends up at
# _internal/EDSDK.framework == camlab.app/Contents/Frameworks/EDSDK.framework (frameworks stay in Frameworks).
# build.sh re-copies the framework from vendor/ with ditto afterwards in case BUNDLE's ad-hoc deep signing
# touched Canon's signature.
edsdk = Tree(str(REPO / "vendor" / "EDSDK.framework"), prefix="EDSDK.framework", excludes=[".DS_Store"])

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    edsdk,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="camlab",
)

app = BUNDLE(
    coll,
    name="camlab.app",
    icon=str(REPO / "assets" / "icon" / "camlab.icns"),
    bundle_identifier="com.dcv.camlab",
    info_plist={
        "CFBundleName": "camlab",
        "CFBundleDisplayName": "camlab",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "NSHumanReadableCopyright": "DCV Applied Technology",
        "NSBluetoothAlwaysUsageDescription": "camlab drives the Godox lights over Bluetooth",
        "NSBluetoothPeripheralUsageDescription": "camlab drives the Godox lights over Bluetooth",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "14.0",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "LSApplicationCategoryType": "public.app-category.photography",
    },
)
