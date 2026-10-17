from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

import pymupdf as fitz

from papertranslator.engine import cached_project_path, project_path, resume_project_path
from papertranslator.extraction import analyze
from papertranslator.layout import export_pdf
from papertranslator.models import Options, Project, dataclass_from
from papertranslator.worker import execute


class ExportOptionsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = patch.dict(os.environ, {"PAPERTRANSLATOR_DATA_DIR": str(self.root / "internal")})
        environment.start()
        self.addCleanup(environment.stop)
        self.source = self.root / "source.pdf"
        with fitz.open() as document:
            page = document.new_page(width=360, height=280)
            page.insert_text((40, 60), "Scientific result", fontsize=12)
            note = page.add_text_annot((250, 150), "Keep the author's note")
            note.set_info(title="Original author")
            note = page.add_text_annot((250, 180), "[待校对] inherited program note")
            note.set_info(title="PaperTranslator 校对提示")
            document.save(self.source)
        self.options = Options(ocr=False, translate_figures=False, vision_review=False)
        self.project = analyze(str(self.source), self.options, str(self.root / "internal" / "seed.json"),
                               threading.Event(), lambda text: None)
        self.project.blocks[0].translation = "科研结果"
        self.project.blocks[0].status = "edited"
        self.project.blocks[0].note("Check this translation")

    def test_review_marks_and_json_are_independent_and_persist_quality_notes(self):
        for marks in (False, True):
            for include_json in (False, True):
                with self.subTest(marks=marks, json=include_json):
                    options = copy.deepcopy(self.options)
                    options.pdf_review_marks, options.export_json = marks, include_json
                    output = self.root / f"output-{marks}-{include_json}.pdf"
                    report = export_pdf(copy.deepcopy(self.project), output, options)
                    self.assertIn(self.project.blocks[0].id, report["needs_review"])
                    with fitz.open(output) as document:
                        titles = [a.info.get("title") for a in document[0].annots() or []]
                        self.assertEqual("PaperTranslator 校对提示" in titles, marks)
                        self.assertIn("Original author", titles)
                        self.assertIn("科研结果", document[0].get_text())
                    self.assertEqual(output.with_suffix(".report.json").exists(), include_json)
                    self.assertEqual(output.with_suffix(".ptrans.json").exists(), include_json)
                    if include_json:
                        saved = Project.load(output.with_suffix(".ptrans.json"))
                        self.assertEqual(saved.output, str(output))
                        self.assertEqual(saved.blocks[0].translation, "科研结果")
                        self.assertTrue(saved.options["export_json"])

    def test_legacy_project_is_copied_into_internal_cache_without_changing_it(self):
        output = self.root / "output"
        legacy = project_path(str(self.source), str(output))
        self.project.save(legacy)
        original = legacy.read_bytes()
        cache = resume_project_path(str(self.source), str(output))
        self.assertEqual(cache, cached_project_path(str(self.source), str(output)))
        self.assertTrue(cache.is_relative_to(self.root / "internal"))
        self.assertEqual(legacy.read_bytes(), original)
        self.assertEqual(Project.load(cache).blocks[0].translation, "科研结果")
        self.assertNotEqual(cache, cached_project_path(str(self.source), str(self.root / "different-output")))

    def test_queued_export_can_produce_only_pdf_and_keep_resume_cache_internal(self):
        output = self.root / "pdf-only"
        output.mkdir()
        cache = cached_project_path(str(self.source), str(output))
        self.project.save(cache)
        messages = queue.Queue()
        request = {"kind": "queue", "mode": "translate", "files": [str(self.source)], "output": str(output),
                   "options": vars(self.options), "provider": {"api_key": "fake-offline-only", "vision": False}, "autoexport": True}
        with patch("papertranslator.api.ChatClient.complete", side_effect=AssertionError("Must reuse saved translations")):
            execute(request, threading.Event(), messages)
        events = []
        while not messages.empty():
            events.append(messages.get_nowait())
        self.assertFalse([v for k, v in events if k == "error"], events)
        self.assertEqual(len(list(output.iterdir())), 1)
        self.assertEqual(next(output.iterdir()).suffix, ".pdf")
        self.assertTrue(Project.load(cache).output)
        self.assertEqual(Project.load(cache).blocks[0].translation, "科研结果")

    def test_legacy_options_default_to_clean_export_without_retranslation(self):
        old = vars(self.options).copy()
        old.pop("pdf_review_marks")
        old.pop("export_json")
        restored = dataclass_from(Options, old)
        self.assertFalse(restored.pdf_review_marks)
        self.assertFalse(restored.export_json)
        from papertranslator.translation import translation_fingerprint
        before = translation_fingerprint(restored, {"model": "offline"})
        restored.pdf_review_marks = restored.export_json = True
        self.assertEqual(before, translation_fingerprint(restored, {"model": "offline"}))


if __name__ == "__main__":
    unittest.main()
