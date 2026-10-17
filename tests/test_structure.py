from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import pymupdf as fitz

from papertranslator.extraction import analyze, native_blocks
from papertranslator.layout import export_pdf
from papertranslator.models import Block, Options, Project, Provider, Unit
from papertranslator.structure import assign_units, figure_rects, join_continuations, merge_ocr_paragraphs
from papertranslator.translation import PLACEHOLDER, translate_project
from papertranslator.api import APIError


class StructureTests(unittest.TestCase):
    def test_pdf_object_continuation_preserves_inline_object_ids(self):
        a = Block("a", 0, [40, 50, 250, 80], "A paragraph with a coefficient", template="A paragraph [[MATH_0]] with a coefficient",
                  inline=[{"marker": "[[MATH_0]]", "text": "x", "rect": [100, 50, 108, 60]}])
        b = Block("b", 0, [40, 82, 250, 110], "continues after another PDF object.", template="continues [[MATH_0]] after another PDF object.",
                  inline=[{"marker": "[[MATH_0]]", "text": "y", "rect": [100, 82, 108, 92]}])
        merged = join_continuations([a, b], [])
        self.assertEqual(len(merged), 1)
        self.assertIn("continues", merged[0].text)
        self.assertEqual([i["marker"] for i in merged[0].inline], ["[[MATH_0]]", "[[MATH_1]]"])
        self.assertIn("[[MATH_1]]", merged[0].template)

    def test_small_header_does_not_merge_into_large_title(self):
        with fitz.open() as doc:
            p = doc.new_page()
            p.insert_text((40, 50), "Article in Press", fontsize=9)
            p.insert_text((350, 50), "https://example.org/article", fontsize=8)
            p.insert_text((40, 85), "A complete academic title", fontsize=24)
            p.insert_text((40, 113), "continued on another line", fontsize=24)
            blocks = native_blocks(p, 0, [])
            title = next(b for b in blocks if "academic title" in b.text)
            self.assertIn("continued", title.text)
            self.assertNotIn("Article in Press", title.text)
            self.assertNotIn("https://", title.text)

    def test_indented_paragraphs_separate_inside_one_pdf_block(self):
        with fitz.open() as doc:
            p = doc.new_page()
            for point, text in [((52, 70), "This is the first paragraph and its beginning."), ((40, 84), "Its second line completes the first paragraph."),
                                ((52, 98), "This is a separate paragraph with a new idea."), ((40, 112), "Its continuation must stay in the second paragraph.")]:
                p.insert_text(point, text, fontsize=11)
            blocks = [b for b in native_blocks(p, 0, []) if not b.protected]
            self.assertEqual(len(blocks), 2)
            self.assertIn("second line", blocks[0].text)
            self.assertIn("continuation", blocks[1].text)
            self.assertNotIn("separate", blocks[0].text)

    def test_columns_do_not_join(self):
        with fitz.open() as doc:
            p = doc.new_page()
            p.insert_textbox((40, 60, 255, 140), "Left paragraph has two lines of scientific prose. Its content stays together.", fontsize=11)
            p.insert_textbox((310, 60, 525, 140), "Right paragraph contains different prose. It must stay in the right column.", fontsize=11)
            blocks = [b for b in native_blocks(p, 0, []) if not b.protected]
            self.assertEqual(len(blocks), 2)
            self.assertFalse(any("Left" in b.text and "Right" in b.text for b in blocks))

    def test_inline_formula_roundtrip_keeps_vector_content(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = root / "source.pdf"
            with fitz.open() as doc:
                p = doc.new_page(width=400, height=300)
                p.insert_text((40, 70), "Consider", fontsize=12)
                p.insert_text((98, 70), "a", fontname="symb", fontsize=12)
                p.insert_text((112, 70), "in the proof.", fontsize=12)
                doc.save(source)
            options = Options(ocr=False, translate_figures=False, vision_review=False)
            project = analyze(str(source), options, str(root / "job.json"), threading.Event(), lambda _: None)
            paragraph = next(b for b in project.blocks if "Consider" in b.text)
            paragraph.translation = "在证明中考虑 [[MATH_0]]。"
            paragraph.status = "translated"
            report = export_pdf(project, root / "translated.pdf", options)
            self.assertFalse(report["needs_review"], report)
            with fitz.open(root / "translated.pdf") as doc:
                text = doc[0].get_text()
                self.assertIn("证明", text)
                self.assertNotIn("Consider", text)
                self.assertNotIn("MATH_", text)
                self.assertTrue(any("Symbol" in row[3] for row in doc[0].get_fonts(full=True)))

    def test_missing_inline_formula_retains_entire_original_paragraph(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = root / "source.pdf"
            with fitz.open() as doc:
                p = doc.new_page(width=400, height=300)
                p.insert_text((40, 70), "Consider", fontsize=12)
                p.insert_text((98, 70), "a", fontname="symb", fontsize=12)
                p.insert_text((112, 70), "in the proof.", fontsize=12)
                doc.save(source)
            options = Options(ocr=False, translate_figures=False)
            project = analyze(str(source), options, str(root / "job.json"), threading.Event(), lambda _: None)
            project.blocks[0].translation = "不完整的译文"
            report = export_pdf(project, root / "translated.pdf", options)
            self.assertTrue(report["needs_review"])
            with fitz.open(root / "translated.pdf") as doc:
                self.assertIn("Consider", doc[0].get_text())

    def test_inline_formula_moves_its_rule_without_removing_chart_lines(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = root / "source.pdf"
            with fitz.open() as doc:
                p = doc.new_page(width=400, height=300)
                p.insert_text((40, 70), "Consider", fontsize=12)
                p.insert_text((98, 70), "a", fontname="symb", fontsize=12)
                p.insert_text((112, 70), "in the proof.", fontsize=12)
                p.draw_line((99, 65), (105, 65), color=(1, 0, 0), width=0.5)
                p.draw_line((50, 150), (300, 150), width=1)
                doc.save(source)
            options = Options(ocr=False, translate_figures=False)
            project = analyze(str(source), options, str(root / "job.json"), threading.Event(), lambda _: None)
            paragraph = next(b for b in project.blocks if "Consider" in b.text)
            paragraph.translation, paragraph.status = "设 [[MATH_0]]。", "translated"
            report = export_pdf(project, root / "translated.pdf", options)
            self.assertFalse(report["needs_review"])
            with fitz.open(root / "translated.pdf") as doc:
                erased = doc[0].get_pixmap(clip=fitz.Rect(99, 64, 105, 66), dpi=144, alpha=False)
                self.assertGreater(min(erased.samples), 240)
                chart = doc[0].get_pixmap(clip=fitz.Rect(100, 149, 200, 151), dpi=144, alpha=False)
                self.assertLess(min(chart.samples), 50)

    def test_figure_groups_labels_and_caption_excluding_body(self):
        options = Options(ocr=False)
        blocks = [Block("a", 0, [50, 160, 130, 175], "Training accuracy"), Block("b", 0, [150, 210, 260, 225], "Validation loss"),
                  Block("c", 0, [40, 310, 250, 340], "Fig. 1 | Training and validation."),
                  Block("body", 0, [40, 70, 260, 110], "A complete body paragraph outside the figure.")]
        project = Project("offline", "", vars(options), selected_pages=[0], blocks=blocks)
        assign_units(project, {0: [[40, 140, 300, 290]]})
        figure = next(u for u in project.units if u.kind == "figure")
        self.assertEqual(set(figure.block_ids), {"a", "b", "c"})
        self.assertEqual(blocks[2].role, "figure_caption")
        self.assertNotEqual(blocks[3].unit_id, figure.id)
        self.assertEqual(sum(u.kind == "paragraph" for u in project.units), 1)

    def test_complete_figure_not_split_at_24_labels(self):
        options = Options(ocr=False, vision_review=False)
        blocks = [Block(f"label{i}", 0, [10, 20, 100, 35], f"Metric name {i}", role="figure_label", unit_id="figure") for i in range(30)]
        unit = Unit("figure", 0, "figure", [0, 0, 300, 300], "Fig. 1", [b.id for b in blocks])
        project = Project("offline", "", vars(options), blocks=blocks, units=[unit], selected_pages=[0])
        client = Mock(provider=Provider(api_key="test"), cancel=threading.Event())
        def complete(messages):
            value = json.loads(messages[-1]["content"])
            self.assertEqual(value["unit"]["kind"], "complete_figure")
            self.assertEqual(len(value["unit"]["labels_and_caption"]), 30)
            return json.dumps({"segments": [{"id": b["id"], "translation": "指标 " + " ".join(PLACEHOLDER.findall(b["source"]))} for b in value["segments"]]})
        client.complete.side_effect = complete
        translate_project(project, options, client, lambda: None, lambda _: None)
        self.assertEqual(client.complete.call_count, 1)
        self.assertTrue(all(b.status == "translated" for b in blocks))

    def test_figure_image_rejected_falls_back_to_shared_text_context(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = root / "source.pdf"
            with fitz.open() as doc:
                doc.new_page(width=300, height=300)
                doc.save(source)
            options = Options(ocr=False, vision_review=True)
            block = Block("label", 0, [40, 40, 140, 55], "Training accuracy", unit_id="figure", role="figure_label")
            unit = Unit("figure", 0, "figure", [20, 20, 250, 200], "Fig. 1", [block.id])
            project = Project(str(source), "", vars(options), blocks=[block], units=[unit], selected_pages=[0])
            client = Mock(provider=Provider(api_key="test"), cancel=threading.Event())
            calls = []
            def complete(messages):
                # Copy before the fallback mutates the content to text.
                calls.append(json.loads(json.dumps(messages[-1]["content"])))
                if len(calls) == 1:
                    raise APIError("images unsupported", kind="http", status_code=400)
                return '{"segments":[{"id":"label","translation":"训练准确率"}]}'
            client.complete.side_effect = complete
            translate_project(project, options, client, lambda: None, lambda _: None)
            self.assertIsInstance(calls[0], list)
            self.assertEqual(calls[0][1]["type"], "image_url")
            self.assertEqual(json.loads(calls[1])["unit"]["kind"], "complete_figure")
            self.assertEqual(block.translation, "训练准确率")
            self.assertTrue(project.warnings)

    def test_ocr_body_lines_merge_but_figure_labels_do_not(self):
        blocks = [Block("a", 0, [40, 50, 240, 64], "First OCR body line", kind="raster"),
                  Block("b", 0, [40, 66, 240, 80], "continues the same paragraph.", kind="raster"),
                  Block("f1", 0, [40, 150, 140, 164], "Training accuracy", kind="raster"),
                  Block("f2", 0, [40, 166, 140, 180], "Validation loss", kind="raster")]
        merged = merge_ocr_paragraphs(blocks, [[30, 140, 260, 250]])
        self.assertEqual(len(merged), 3)
        self.assertIn("continues", merged[0].text)
        self.assertEqual(merged[0].rect, [40.0, 50.0, 240.0, 80.0])
        self.assertEqual(merged[1].text, "Training accuracy")

    def test_figure_ocr_runs_even_when_scan_ocr_disabled(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = root / "source.pdf"
            with fitz.open() as image_doc:
                p = image_doc.new_page(width=300, height=180)
                p.insert_text((30, 60), "Training accuracy", fontsize=18)
                png = p.get_pixmap().tobytes("png")
            with fitz.open() as doc:
                p = doc.new_page(width=400, height=600)
                p.insert_image((40, 180, 340, 360), stream=png)
                p.insert_text((40, 385), "Fig. 1 | Model evaluation.", fontsize=11)
                doc.save(source)
            ocr = Mock()
            ocr.recognize.return_value = [Block("temp", 0, [70, 220, 250, 240], "Training accuracy", kind="raster")]
            options = Options(ocr=False, translate_figures=True)
            project = analyze(str(source), options, str(root / "job.json"), threading.Event(), lambda _: None, ocr=ocr)
            self.assertEqual(ocr.recognize.call_count, 1)
            self.assertIn("clip", ocr.recognize.call_args.kwargs)
            figure = next(u for u in project.units if u.kind == "figure")
            self.assertTrue(any(b.id in figure.block_ids and b.text == "Training accuracy" for b in project.blocks))

    def test_fullpage_scan_is_not_a_single_figure(self):
        with fitz.open() as doc:
            p = doc.new_page(width=300, height=400)
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 300, 400), False)
            pix.clear_with(255)
            p.insert_image(p.rect, stream=pix.tobytes("png"))
            self.assertEqual(figure_rects(p), [])


if __name__ == "__main__":
    unittest.main()
