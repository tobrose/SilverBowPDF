# PyInstaller spec for SilverbowPDF.
#
# Build:  pyinstaller packaging/pdfapp.spec --noconfirm
#
# onedir, not onefile, on purpose. onefile unpacks the whole Qt runtime to a
# temp directory on every launch: several seconds of startup, and on a
# locked-down corporate box it looks exactly like malware staging itself --
# AppLocker blocks it, Defender rescans ~200MB every time. onedir installs once
# and launches immediately.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

PROJECT_ROOT = Path(SPECPATH).parent  # noqa: F821 -- SPECPATH is injected

datas = []
binaries = []

# --- pypdfium2 -------------------------------------------------------------
# Two things PyInstaller's autodetection misses, both verified by reading the
# installed package rather than guessing:
#
#   1. The pdfium binary lives in the SEPARATE `pypdfium2_raw` package
#      (pdfium.dll on Windows, ~7MB), not in `pypdfium2`.
#   2. BOTH packages read a `version.json` from a path relative to their own
#      __file__ at import time. Miss either one and the app dies on `import
#      pypdfium2` with a FileNotFoundError that says nothing useful.
datas += collect_data_files("pypdfium2", includes=["version.json"])
datas += collect_data_files("pypdfium2_raw", includes=["version.json"])
binaries += collect_dynamic_libs("pypdfium2_raw")

# --- our own -----------------------------------------------------------------
# Window/taskbar icon, read at runtime from beside pdfapp/__init__ (see
# pdfapp.ICON_FILE). The exe's own icon is packaging/icon.ico, below.
datas += [(str(PROJECT_ROOT / "pdfapp" / "icon.png"), "pdfapp")]

# --- winrt (Windows OCR) -----------------------------------------------------
# ocr.py imports winrt lazily, and the compiled _winrt_* modules import each
# other's Python wrappers from C (the OCR module needs Foundation's to await
# anything), which the analysis can't see. Take the whole (small, ~3MB)
# namespace, plus the msvcp140.dll it ships beside them.
winrt_modules = collect_submodules("winrt")
binaries += collect_dynamic_libs("winrt")

# --- pikepdf ---------------------------------------------------------------
# Native _core extension plus the qpdf runtime it links against.
binaries += collect_dynamic_libs("pikepdf")

# --- exclusions ------------------------------------------------------------
# PySide6 ships the entire Qt framework. Unfiltered, this bundle is 250MB+.
# Everything below is confirmed unused by this app.
#
# QtPdf is excluded deliberately and it is not a mistake: Qt's PDF module
# bundles its OWN copy of pdfium. Leaving it in ships two pdfium builds in one
# installer.
EXCLUDED_QT = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "PySide6.QtHelp", "PySide6.QtSensors", "PySide6.QtTextToSpeech",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtNetworkAuth", "PySide6.QtHttpServer", "PySide6.QtChartsQml",
]

# pypdfium2 imports PIL and numpy lazily (verified: neither is in sys.modules
# after `import pypdfium2`), and rendering.py never calls to_pil/to_numpy, so
# excluding them is safe. If a future feature calls either, remove the exclude
# or you get an ImportError at the moment the user clicks the button.
EXCLUDED_OTHER = [
    "numpy", "PIL", "Pillow", "matplotlib", "scipy", "pandas",
    "tkinter", "pytest", "setuptools", "pip",
]

# NOT excluded, on purpose: QtNetwork and QtOpenGL. QtWidgets links against
# them on some platforms and the failure mode is a silent blank window.
# They're small. Leave them.

block_cipher = None

a = Analysis(  # noqa: F821
    [str(PROJECT_ROOT / "pdfapp" / "__main__.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["pdfapp", "pypdfium2_raw", *winrt_modules],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDED_QT + EXCLUDED_OTHER,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Excluding the Python modules above doesn't stop their DLLs arriving as
# dependencies of Qt plugins: the imageformats/qpdf plugin drags in Qt6Pdf
# (Qt's own pdfium copy -- the thing the QtPdf exclusion was meant to stop),
# and Quick/Qml ride in the same way. ~16MB this app never loads. The frozen
# self-test in build.ps1 is what proves nothing needed went with them.
DROP_BINARIES = ("Qt6Pdf", "Qt6Quick", "Qt6Qml", "qpdf.dll")
a.binaries = [  # noqa: F821
    b for b in a.binaries  # noqa: F821
    if not any(Path(b[0]).name.startswith(d) or Path(b[0]).name == d for d in DROP_BINARIES)
]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SilverbowPDF",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed binaries trip heuristic AV. Not worth the MB.
    console=False,  # GUI app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "packaging" / "icon.ico")
    if (PROJECT_ROOT / "packaging" / "icon.ico").exists()
    else None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SilverbowPDF",
)
