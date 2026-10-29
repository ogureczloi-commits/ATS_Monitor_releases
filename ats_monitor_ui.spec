# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import (
    collect_all,
    collect_dynamic_libs,
    collect_submodules,
)

# Максимально явный сбор NumPy/OpenCV для чистых Windows-машин.
# Важно для ошибки вида:
# ImportError: DLL load failed while importing _multiarray_umath
numpy_datas, numpy_binaries_all, numpy_hidden_all = collect_all('numpy')
cv2_datas, cv2_binaries_all, cv2_hidden_all = collect_all('cv2')
mpl_datas, mpl_binaries_all, mpl_hidden_all = collect_all('matplotlib')

numpy_binaries = numpy_binaries_all + collect_dynamic_libs('numpy')
cv2_binaries = cv2_binaries_all + collect_dynamic_libs('cv2')
mpl_binaries = mpl_binaries_all + collect_dynamic_libs('matplotlib')

numpy_hidden = numpy_hidden_all + collect_submodules('numpy')
cv2_hidden = cv2_hidden_all + collect_submodules('cv2')
mpl_hidden = mpl_hidden_all + collect_submodules('matplotlib')


a = Analysis(
    ['ats_monitor_ui.py'],
    pathex=[],
    binaries=numpy_binaries + cv2_binaries + mpl_binaries,
    datas=numpy_datas + cv2_datas + mpl_datas + [
        ('icons', 'icons'),
        ('sounds', 'sounds'),
        ('ats_monitor_local_config.example.json', '.'),
        ('VERSION', '.'),
        ('CHANGELOG.md', '.'),
    ],
    hiddenimports=numpy_hidden + cv2_hidden + mpl_hidden + [
        'psycopg2',
        'psycopg2._psycopg',
        'psycopg2.extensions',
        'psycopg2.extras',
        'PIL._tkinter_finder',
    ],
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
    [],
    exclude_binaries=True,
    name='ats_monitor_ui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ats_monitor_ui',
)
