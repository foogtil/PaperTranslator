# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules, collect_data_files, collect_dynamic_libs

root = Path(SPECPATH)
datas, binaries, hiddenimports = [], [], []
for package in ('rapidocr_onnxruntime', 'tkinterdnd2'):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden
hiddenimports += collect_submodules('rapidocr_onnxruntime')
datas += collect_data_files('onnxruntime', excludes=['tools/**', 'transformers/**', 'datasets/**', 'quantization/**'])
binaries += collect_dynamic_libs('onnxruntime')
hiddenimports += ['onnxruntime.capi._pybind_state', 'onnxruntime.capi.onnxruntime_inference_collection']
hiddenimports += ['PIL.ImageTk', 'pymupdf', 'httpx', 'httpcore', 'certifi', 'cv2', 'shapely']
for name in ('README.md', 'README.zh-CN.md', 'DEFAULTS.md', 'LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md'):
    datas.append((str(root / name), '.'))
datas.append((str(root / 'licenses'), 'licenses'))
datas.append((str(root / 'assets'), 'assets'))

a = Analysis(
    [str(root / 'run.py')], pathex=[str(root)], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports,
    excludes=['torch','torchvision','tensorflow','transformers','sklearn','numba','llvmlite','nltk','pyarrow','huggingface_hub','tokenizers','datasets','onnx','onnxruntime.tools','onnxruntime.transformers','onnxruntime.quantization','onnxruntime.datasets','sympy','pandas','scipy','matplotlib','IPython','jupyter','pytest','PyQt5','PyQt6','PySide6','pypdfium2','pdfplumber','openpyxl','docx'],
    noarchive=False,
)
pyz = PYZ(a.pure)
portable_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='PaperTranslator', debug=False,
                  bootloader_ignore_signals=False, strip=False, upx=False, console=False,
                  disable_windowed_traceback=False, icon=str(root / 'assets' / 'icon.ico'))
collection = COLLECT(portable_exe, a.binaries, a.datas, strip=False, upx=False, name='PaperTranslator')
standalone = EXE(pyz, a.scripts, a.binaries, a.datas, name='PaperTranslator-Standalone', debug=False,
                 bootloader_ignore_signals=False, strip=False, upx=False, console=False,
                 disable_windowed_traceback=False, icon=str(root / 'assets' / 'icon.ico'))
