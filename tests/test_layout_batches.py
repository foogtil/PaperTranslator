from __future__ import annotations

import math
from pathlib import Path
import tempfile
import threading
import unittest

import pymupdf as fitz

from papertranslator.layout import _add_redaction_quads, export_pdf
from papertranslator.models import Block, Cancelled, Options, PageInfo, Project, digest_file


class LayoutBatchTests(unittest.TestCase):
    def fixture(self, crop=False, angle=0):
        document = fitz.open()
        page = document.new_page(width=400, height=300)
        page.insert_text((80, 140), "Alpha KEEP Omega", fontsize=15, rotate=angle)
        page.draw_line((65, 160), (340, 160), width=1)
        if crop:
            page.set_cropbox(fitz.Rect(40, 30, 360, 280))
        chars = [c for b in page.get_text("rawdict")["blocks"] if b.get("type") == 0
                 for line in b["lines"] for span in line["spans"] for c in span["chars"]]
        rectangles = []
        for char in chars:
            if char["c"] in "AO":
                rect = fitz.Rect(char["bbox"])
                x, y = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
                rectangles.append([x - 0.025, y - 0.025, x + 0.025, y + 0.025])
        return document, rectangles

    def test_batched_disjoint_quads_match_individual_glyph_erasers(self):
        for crop, angle in ((False, 0), (True, 0), (False, 90), (False, 270)):
            with self.subTest(crop=crop, angle=angle):
                source, rectangles = self.fixture(crop, angle)
                with source, fitz.open(stream=source.tobytes()) as original, fitz.open(stream=source.tobytes()) as batch:
                    self.assertEqual(len(rectangles), 2)
                    for rect in rectangles:
                        original[0].add_redact_annot(rect, fill=False, cross_out=False)
                    _add_redaction_quads(batch[0], rectangles)
                    for document in (original, batch):
                        document[0].apply_redactions(images=0, graphics=0, text=0)
                    self.assertEqual(original[0].get_text(), batch[0].get_text())
                    self.assertIn("KEEP", batch[0].get_text())
                    self.assertEqual(original[0].get_pixmap().samples, batch[0].get_pixmap().samples)

    def test_thousands_of_glyph_regions_use_bounded_annotations(self):
        document, rectangles = self.fixture()
        with document:
            count = 5000
            _add_redaction_quads(document[0], rectangles * (count // 2))
            self.assertEqual(len(list(document[0].annots())), math.ceil(count / 256))
            document[0].apply_redactions(images=0, graphics=0, text=0)
            self.assertIn("KEEP", document[0].get_text())

    def test_batched_eraser_creation_checks_cancellation(self):
        document, rectangles = self.fixture()
        cancel = threading.Event()
        with document, self.assertRaises(Cancelled):
            _add_redaction_quads(document[0], rectangles * 300, cancel,
                                 on_batch=lambda done, total: cancel.set())

    def test_export_reports_preparation_page_fonts_save_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.pdf"
            document, rectangles = self.fixture()
            with document:
                document.save(path)
            block = Block("b1", 0, [75, 115, 240, 145], "Alpha Omega", erasers=rectangles,
                          translation="测试译文", status="translated")
            project = Project(str(path), digest_file(path), {}, pages=[PageInfo(0, 400, 300, 0)],
                              blocks=[block], selected_pages=[0])
            events = []
            export_pdf(project, Path(directory) / "translated.pdf", Options(), progress=events.append)
            phases = {event["phase"] for event in events}
            self.assertTrue({"prepare", "typeset", "prepare_erasers", "erase_text", "write_text",
                             "page_done", "fonts", "save", "verify", "report", "complete"} <= phases)
            self.assertEqual(events[-1]["phase"], "complete")
            self.assertTrue(all(event["current"] <= event["total"] for event in events))
            self.assertTrue(any(event["temporary"] for event in events))


if __name__ == "__main__":
    unittest.main()
