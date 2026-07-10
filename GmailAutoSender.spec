# PyInstaller spec — build with: python -m PyInstaller GmailAutoSender.spec
# Produces a single self-contained dist/GmailAutoSender.exe (no console window).

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["paths", "gmail_client", "sender", "ui", "exceptions"],
    hookspath=[],
    runtime_hooks=[],
    # Trim large libraries the app never touches to keep the exe small.
    excludes=[
        "numpy", "pandas", "matplotlib", "scipy", "PIL",
        "pytest", "PyQt5", "PySide2", "test", "unittest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="GmailAutoSender",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # GUI app — no terminal window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
