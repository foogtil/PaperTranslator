from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys

from .models import atomic_json


def data_dir() -> Path:
    override = os.environ.get("PAPERTRANSLATOR_DATA_DIR")
    root = Path(override) if override else Path(os.environ.get("APPDATA", Path.home() / ".config")) / "PaperTranslator"
    root.mkdir(parents=True, exist_ok=True)
    return root


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("此系统不支持 Windows 密钥加密；请取消记住密钥。")
    memory = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(memory, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt.CryptProtectData.restype = wintypes.BOOL
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN. Windows current-user scope, never machine-wide.
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), "PaperTranslator", None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


def seal(secret: str) -> str:
    return base64.b64encode(_dpapi(secret.encode("utf-8"))).decode("ascii")


def unseal(secret: str) -> str:
    return _dpapi(base64.b64decode(secret), decrypt=True).decode("utf-8")


def load_settings() -> dict:
    path = data_dir() / "settings.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            result = json.load(handle)
        for profile in result.get("profiles", {}).values():
            cipher = profile.pop("key_dpapi", "")
            if cipher:
                try:
                    profile["api_key"] = unseal(cipher)
                except Exception:
                    profile["api_key"] = ""
        return result
    except (ValueError, OSError):
        return {}


def save_settings(settings: dict, remember: bool = False):
    result = json.loads(json.dumps(settings))
    for profile in result.get("profiles", {}).values():
        secret = profile.pop("api_key", "")
        profile.pop("key_dpapi", None)
        if remember and secret:
            profile["key_dpapi"] = seal(secret)
    atomic_json(data_dir() / "settings.json", result)
