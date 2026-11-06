from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import string
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from papertranslator.localization import (DOCUMENT_UI_LANGUAGES, UI_LANGUAGES, Localizer,
                                         LocalizedVar, catalogs, resolve_language)
from papertranslator.models import LANGUAGES
from papertranslator.settings import save_ui_language


class LocalizationTests(unittest.TestCase):
    def test_every_document_language_has_a_ui_language_and_complete_catalog(self):
        self.assertEqual(set(DOCUMENT_UI_LANGUAGES), set(LANGUAGES))
        self.assertEqual(set(DOCUMENT_UI_LANGUAGES.values()), set(UI_LANGUAGES))
        self.assertEqual(resolve_language(None), "zh-CN")
        source = catalogs()["en"]
        for code in UI_LANGUAGES:
            if code in ("auto", "zh-TW"):
                continue
            with self.subTest(code=code):
                self.assertEqual(set(catalogs()[code]), set(source))
                for message, translation in catalogs()[code].items():
                    fields = lambda s: {f for _, f, _, _ in string.Formatter().parse(s) if f is not None}
                    self.assertEqual(fields(message), fields(translation), (code, message))

    def test_static_ui_labels_are_covered(self):
        gui = Path(__file__).resolve().parents[1] / "papertranslator" / "gui.py"
        tree = ast.parse(gui.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                value = keyword.value
                if keyword.arg == "text" and isinstance(value, ast.Constant) and isinstance(value.value, str) and re.search("[\u4e00-\u9fff]", value.value):
                    self.assertIn(value.value, catalogs()["en"])

    def test_system_language_resolution_and_invalid_preference(self):
        for name, expected in [("en_US", "en"), ("Japanese_Japan", "ja"), ("zh_Hant_TW", "zh-TW"),
                               ("Chinese (Traditional)_Taiwan", "zh-TW"), ("ar_SA", "ar"), ("hi_IN", "hi"), ("zz_ZZ", "zh-CN")]:
            with self.subTest(name=name), patch("papertranslator.localization.locale.getlocale", return_value=(name, "UTF-8")):
                self.assertEqual(resolve_language("auto"), expected)
        self.assertEqual(resolve_language("unknown"), "zh-CN")

    def test_ui_save_patches_only_preference_without_touching_key_or_project_settings(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"PAPERTRANSLATOR_DATA_DIR": directory}):
            path = Path(directory) / "settings.json"
            original = {"profiles": {"MiMo": {"key_dpapi": "opaque-encrypted-value", "model": "user-model"}},
                        "remember": True, "options": {"source_language": "英语", "target_language": "简体中文"}, "extension": [1, 2]}
            path.write_text(json.dumps(original), encoding="utf-8")
            with patch("papertranslator.settings.unseal", side_effect=AssertionError("Must not decrypt")), \
                    patch("papertranslator.settings.seal", side_effect=AssertionError("Must not encrypt")):
                save_ui_language("ja")
            updated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(updated.pop("ui_language"), "ja")
            self.assertEqual(updated, original)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                save_ui_language("unsupported")
            self.assertEqual(path.read_bytes(), before)
            path.write_text("invalid JSON", encoding="utf-8")
            with self.assertRaises(ValueError):
                save_ui_language("en")
            self.assertEqual(path.read_text(encoding="utf-8"), "invalid JSON")

    def test_localized_choices_keep_canonical_language_and_provider_values(self):
        interpreter = tk.Tcl()
        for code in UI_LANGUAGES:
            if code == "auto":
                continue
            tr = Localizer(code)
            value = LocalizedVar(interpreter, tr, "英语")
            self.assertEqual(value.get(), "英语")
            self.assertEqual(interpreter.getvar(str(value)), tr("英语"))
            interpreter.setvar(str(value), tr("法语"))
            self.assertEqual(value.get(), "法语")
            value.set("自定义")
            self.assertEqual(value.get(), "自定义")
            self.assertEqual(interpreter.getvar(str(value)), tr("自定义"))
            del value

    def test_dynamic_messages_keep_data_and_translate_nested_templates(self):
        tr = Localizer("en")
        self.assertEqual(tr("接口连接成功，模型返回：OK"), "API connected. Model returned: OK")
        self.assertEqual(tr("区域：native · 字号 12.5 pt"), "Region: native · font 12.5 pt")
        opaque = "C:/papers/数学公式-2026.pdf"
        for code in UI_LANGUAGES:
            self.assertEqual(Localizer(code)(opaque), opaque)
        self.assertEqual(tr("排版 · 第 3 页 · 写回译文 · 此步 5 秒"), "Layout · Page 3 · Write translation · step 5 s")
        self.assertIn("設置", Localizer("zh-TW")("设置已保存。"))


if __name__ == "__main__":
    unittest.main()
