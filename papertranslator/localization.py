"""Offline UI translations. Document language and project data stay canonical."""
from __future__ import annotations

import csv
import ctypes
from functools import lru_cache
import locale
from pathlib import Path
import re
import string
import sys
import tkinter as tk


UI_LANGUAGES = {
    "zh-CN": "简体中文", "zh-TW": "繁體中文", "en": "English", "ja": "日本語",
    "ko": "한국어", "de": "Deutsch", "fr": "Français", "es": "Español",
    "pt": "Português", "ru": "Русский", "ar": "العربية", "hi": "हिन्दी",
    "auto": "跟随系统 / System",
}
DOCUMENT_UI_LANGUAGES = {
    "自动检测": "auto", "英语": "en", "简体中文": "zh-CN", "繁体中文": "zh-TW",
    "日语": "ja", "韩语": "ko", "德语": "de", "法语": "fr", "西班牙语": "es",
    "葡萄牙语": "pt", "俄语": "ru", "阿拉伯语": "ar", "印地语": "hi",
}


def resolve_language(code):
    if code == "auto":
        name = (locale.getlocale()[0] or "").replace("_", "-").lower()
        if name.startswith("zh") or "chinese" in name:
            return "zh-TW" if any(part in name for part in ("tw", "hk", "hant", "traditional")) else "zh-CN"
        windows_names = {"english": "en", "japanese": "ja", "korean": "ko", "german": "de", "french": "fr", "spanish": "es", "portuguese": "pt", "russian": "ru", "arabic": "ar", "hindi": "hi"}
        for prefix, language in windows_names.items():
            if name.startswith(prefix):
                return language
        code = name.split("-")[0]
    return code if code in UI_LANGUAGES and code != "auto" else "zh-CN"


@lru_cache(maxsize=1)
def catalogs():
    result = {code: {} for code in UI_LANGUAGES if code not in ("auto", "zh-TW")}
    with Path(__file__).with_name("locales.tsv").open(encoding="utf-8", newline="") as stream:
        rows = csv.DictReader(stream, delimiter="\t")
        for row in rows:
            if None in row or any(not value for value in row.values()):
                raise ValueError("Incomplete UI translation row")
            source = row["zh-CN"].replace("\\n", "\n")
            if source in result["en"]:
                raise ValueError("Duplicate UI translation: " + source)
            for code, value in row.items():
                result[code][source] = value.replace("\\n", "\n")
    return result


@lru_cache(maxsize=4096)
def traditional(text):
    if sys.platform != "win32":
        return text
    convert = ctypes.windll.kernel32.LCMapStringEx
    convert.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_int,
                        ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t]
    convert.restype = ctypes.c_int
    size = convert("zh-TW", 0x04000000, text, -1, None, 0, None, None, 0)
    if not size:
        return text
    output = ctypes.create_unicode_buffer(size)
    if not convert("zh-TW", 0x04000000, text, -1, output, size, None, None, 0):
        return text
    return output.value


class Localizer:
    def __init__(self, language="zh-CN"):
        self.language = resolve_language(language)
        self.messages = catalogs().get(self.language, {})
        if self.language == "zh-TW":
            self.messages = {key: traditional(key) for key in catalogs()["en"]}
        self.reverse = {value: key for key, value in self.messages.items()}
        self.patterns = []
        for source, target in self.messages.items():
            if "{" not in source:
                continue
            pattern, fields = "", []
            for literal, field, spec, conversion in string.Formatter().parse(source):
                pattern += re.escape(literal)
                if field is not None:
                    fields.append(field)
                    pattern += "(.+?)"
            self.patterns.append((re.compile(pattern, re.DOTALL), fields, target))

    def __call__(self, text):
        text = str(text)
        if self.language == "zh-CN":
            return text
        if text in self.messages:
            return self.messages[text]
        if "\n" in text:
            return "\n".join(self(line) for line in text.split("\n"))
        for pattern, fields, target in self.patterns:
            match = pattern.fullmatch(text)
            if match:
                return target.format_map({name: self(value) for name, value in zip(fields, match.groups())})
        return text


class LocalizedVar(tk.StringVar):
    """Display localized text while code and editable choices use original values."""
    def __init__(self, master, localizer, value=""):
        self.localizer = localizer
        self.original = str(value)
        self.display = localizer(value)
        super().__init__(master, value=self.display)

    def set(self, value):
        self.original = str(value)
        self.display = self.localizer(value)
        super().set(self.display)

    def get(self):
        value = super().get()
        return self.original if value == self.display else self.localizer.reverse.get(value, value)


class LocalizedDialogs:
    def __init__(self, module, localizer):
        self.module, self.localizer = module, localizer

    def __getattr__(self, name):
        def call(*args, **kwargs):
            args = tuple(self.localizer(value) if isinstance(value, str) else value for value in args)
            for key in ("title", "message", "prompt"):
                if key in kwargs:
                    kwargs[key] = self.localizer(kwargs[key])
            if "filetypes" in kwargs:
                kwargs["filetypes"] = [(self.localizer(title), pattern) for title, pattern in kwargs["filetypes"]]
            return getattr(self.module, name)(*args, **kwargs)
        return call


def localize_widgets(root, localizer):
    for widget in root.winfo_children():
        if "text" in widget.keys():
            text = widget.cget("text")
            if text:
                widget.configure(text=localizer(text))
        if widget.winfo_class() == "TNotebook":
            for tab in widget.tabs():
                widget.tab(tab, text=localizer(widget.tab(tab, "text")))
        elif widget.winfo_class() == "Treeview":
            for column in ("#0", *widget.cget("columns")):
                widget.heading(column, text=localizer(widget.heading(column, "text")))
        localize_widgets(widget, localizer)
