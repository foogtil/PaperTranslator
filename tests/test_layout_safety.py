from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import threading
import unittest

import pymupdf as fitz

from papertranslator.extraction import analyze, native_blocks
from papertranslator.flow import flow_regions, local_obstacles
from papertranslator.layout import export_pdf, _text_page, _conflicting_placements
from papertranslator.layout_upgrade import refresh_layout, EXTRACTION_VERSION
from papertranslator.models import Block, Cancelled, Options, PageInfo, Project, digest_file


class LayoutSafetyTests(unittest.TestCase):
    def test_wide_paragraph_owns_hat_subscript_and_upright_model_name(self):
        with fitz.open() as doc:
            p = doc.new_page(width=650, height=250)
            p.insert_text((40, 60), "We calculate residuals using", fontsize=11)
            p.insert_text((180, 60), "Y", fontname="tiit", fontsize=11)
            p.insert_text((182, 57), "^", fontsize=9)
            p.insert_text((188, 62), "t", fontname="tiit", fontsize=7)
            p.insert_text((191, 55), "MODEL", fontsize=7)
            p.insert_text((222, 60), "for all samples in the training set and all future values.", fontsize=11)
            blocks = native_blocks(p, 0)
            paragraph = next(b for b in blocks if "residuals" in b.text)
            self.assertGreater(paragraph.rect[2] - paragraph.rect[0], 350)
            self.assertEqual(len(paragraph.inline), 1)
            self.assertIn("MODEL", paragraph.inline[0]["text"])
            self.assertNotIn("for all", paragraph.inline[0]["text"])
            self.assertTrue(any("^" in m["text"] for m in paragraph.inline))
            self.assertFalse(any(b is not paragraph and "MODEL" in b.text for b in blocks))

    def test_flow_frames_exclude_photograph_and_continue_below(self):
        photo = fitz.Rect(0, 0, 70, 90)
        regions = flow_regions(fitz.Rect(0, 0, 240, 170), [photo])
        self.assertEqual(regions, [fitz.Rect(70, 0, 240, 90), fitz.Rect(0, 90, 240, 170)])
        self.assertTrue(all((r & photo).is_empty for r in regions))

    def test_obstacles_follow_rotated_paragraph_coordinates(self):
        obstacle = fitz.Rect(10, 20, 40, 60)
        expected = {0: (0, 0, 30, 40), 90: (160, 0, 200, 30),
                    180: (70, 160, 100, 200), 270: (0, 70, 40, 100)}
        for angle, box in expected.items():
            b = Block("b", 0, [10, 20, 110, 220], "text", angle=angle)
            self.assertEqual(local_obstacles(b, [obstacle]), [fitz.Rect(box)])

    def photo_fixture(self, root, rotation=0):
        source = root / "source.pdf"
        with fitz.open() as doc:
            p = doc.new_page(width=340, height=300)
            photo = fitz.Rect(30, 40, 105, 130)
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 50, 60), False)
            pix.clear_with(80)
            p.insert_image(photo, stream=pix.tobytes("png"))
            for y in (55, 70, 85, 100, 115):
                p.insert_text((115, y), "Biography beside the photograph.", fontsize=9)
            p.insert_text((30, 145), "This same paragraph continues below the photograph.", fontsize=9)
            p.insert_text((30, 160), "The complete translation must remain one paragraph.", fontsize=9)
            p.set_rotation(rotation)
            doc.save(source)
        options = Options(ocr=False, translate_figures=False, vision_review=False)
        project = analyze(str(source), options, str(root / "job.json"), threading.Event(), lambda _: None)
        return source, project, options, photo

    def test_wrapped_paragraph_keeps_photograph_pixels_and_full_text(self):
        for rotation in (0, 90, 270):
            with self.subTest(rotation=rotation), tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
                root = Path(directory)
                source, project, options, photo = self.photo_fixture(root, rotation)
                # A single complete unit spanning narrow and wide physical lines.
                prose = [b for b in project.blocks if not b.protected]
                combined = copy.deepcopy(prose[0])
                rect = fitz.Rect(prose[0].rect)
                for b in prose[1:]:
                    rect |= fitz.Rect(b.rect)
                    combined.erasers.extend(b.erasers)
                    combined.glyphs.extend(b.glyphs)
                combined.rect = list(rect)
                combined.translation = "这是一段围绕照片排版的完整作者介绍。" * 12 + "完整结尾。"
                combined.status = "translated"
                project.blocks = [combined]
                output = root / "out.pdf"
                report = export_pdf(project, output, options)
                self.assertEqual(report["needs_review"], [])
                with fitz.open(source) as original, fitz.open(output) as result:
                    self.assertEqual(result[0].rotation, rotation)
                    original[0].set_rotation(0)
                    result[0].set_rotation(0)
                    self.assertEqual(original[0].get_pixmap(clip=photo, dpi=144).samples,
                                     result[0].get_pixmap(clip=photo, dpi=144).samples)
                    self.assertEqual("".join(result[0].get_text().split()), "".join(combined.translation.split()))
                    glyphs = [c for b in result[0].get_text("rawdict")["blocks"] if b.get("type") == 0
                              for l in b["lines"] for s in l["spans"] for c in s["chars"] if not c["c"].isspace()]
                    self.assertTrue(all((fitz.Rect(c["bbox"]) & photo).is_empty for c in glyphs))

    def test_impossible_wrapping_keeps_original_and_translation_for_review(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source, project, options, photo = self.photo_fixture(root)
            b = next(b for b in project.blocks if not b.protected)
            b.translation, b.status = "极长的完整译文" * 1000, "translated"
            report = export_pdf(project, root / "out.pdf", options)
            self.assertIn(b.id, report["needs_review"])
            self.assertEqual(b.status, "overflow")
            self.assertTrue(b.translation.endswith("完整译文"))
            with fitz.open(source) as original, fitz.open(root / "out.pdf") as output:
                self.assertEqual(original[0].get_pixmap().samples, output[0].get_pixmap().samples)

    def test_ambiguous_ocr_patch_cannot_erase_a_photograph(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source, project, options, photo = self.photo_fixture(root)
            b = project.blocks[0]
            b.rect = [30, 40, 300, 170]
            b.kind, b.role, b.confidence = 'raster', 'paragraph', 1.0
            b.translation, b.status = '完整作者介绍。' * 10, 'translated'
            project.blocks = [b]
            report = export_pdf(project, root / 'out.pdf', options)
            self.assertIn(b.id, report['needs_review'])
            with fitz.open(source) as original, fitz.open(root / 'out.pdf') as output:
                self.assertEqual(original[0].get_pixmap().samples, output[0].get_pixmap().samples)

    def test_figure_label_translation_is_allowed_inside_its_own_image(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = root / "source.pdf"
            with fitz.open() as doc:
                p = doc.new_page(width=300, height=200)
                pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 60), False)
                pix.clear_with(240)
                p.insert_image((20, 20, 280, 170), stream=pix.tobytes("png"))
                p.insert_text((40, 60), "Training accuracy", fontsize=12)
                doc.save(source)
            options = Options(ocr=False, translate_figures=False)
            project = analyze(str(source), options, str(root / "job.json"), threading.Event(), lambda _: None)
            b = next(b for b in project.blocks if not b.protected)
            self.assertEqual(b.role, "figure_label")
            options.translate_figures = True
            b.translation, b.status = "训练准确率", "translated"
            report = export_pdf(project, root / "out.pdf", options)
            self.assertFalse(report["needs_review"])
            with fitz.open(root / "out.pdf") as result:
                self.assertIn("准确率", result[0].get_text())

    def test_upgrade_reuses_unchanged_translations_and_blocks_stale_formula_draft(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source, project, options, _ = self.photo_fixture(root)
            first = project.blocks[0]
            first.translation, first.status = "已有完整译文", "translated"
            second = project.blocks[1]
            second.template = "Old [[MATH_0]] template"
            second.inline = [{"marker": "[[MATH_0]]", "rect": [1, 2, 3, 4], "text": "x"}]
            second.translation, second.status = "旧公式 [[MATH_0]] 译文", "translated"
            project.extraction_version = 3
            with fitz.open(source) as doc:
                refresh_layout(project, doc)
            self.assertEqual(project.extraction_version, EXTRACTION_VERSION)
            self.assertTrue(any(b.translation == "已有完整译文" and b.status == "translated" for b in project.blocks))
            stale = next(b for b in project.blocks if b.status == "layout_pending")
            self.assertIn("旧公式", stale.translation)
            report = export_pdf(project, root / "out.pdf", options)
            self.assertIn(stale.id, report["needs_review"])
            with fitz.open(root / "out.pdf") as result:
                self.assertNotIn("MATH_", result[0].get_text())

    def test_export_checks_cancellation_during_preflight(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            _, project, options, _ = self.photo_fixture(root)
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(Cancelled):
                export_pdf(project, root / "out.pdf", options, cancel=cancel)
            self.assertFalse((root / "out.pdf").exists())

    def test_failed_retry_cannot_export_a_stale_layout_draft(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source, project, options, _ = self.photo_fixture(root)
            b = project.blocks[0]
            b.translation, b.status, b.layout_stale = "旧结构的译文", "failed", True
            report = export_pdf(project, root / "out.pdf", options)
            self.assertIn(b.id, report["needs_review"])
            with fitz.open(source) as original, fitz.open(root / "out.pdf") as output:
                self.assertEqual(original[0].get_pixmap().samples, output[0].get_pixmap().samples)

    def test_fractional_formula_dimensions_and_text_baseline_survive_export(self):
        with fitz.open() as source, fitz.open() as scratch:
            p = source.new_page(width=400, height=200)
            p.insert_text((40, 70), "Consider", fontsize=12)
            p.insert_text((98, 70), "a", fontname="symb", fontsize=12)
            p.insert_text((112, 70), "in the proof.", fontsize=12)
            b = next(b for b in native_blocks(p, 0) if not b.protected)
            b.translation = "设 [[MATH_0]]，可得结果。"
            placement = _text_page(scratch, b, Options(), source)
            self.assertIsNotNone(placement)
            page = scratch[placement[0]]
            image = page.get_image_info()[0]
            original_rect, image_rect = fitz.Rect(b.inline[0]["rect"]), fitz.Rect(image["bbox"])
            self.assertAlmostEqual(image_rect.width / original_rect.width, image_rect.height / original_rect.height, places=4)
            spans = [s for block in page.get_text("dict")["blocks"] if block.get("type") == 0
                     for line in block["lines"] for s in line["spans"]]
            formula = next(s for s in spans if 'Symbol' in s['font'])
            prose = next(s for s in spans if '设' in s['text'])
            self.assertAlmostEqual(formula['origin'][1], prose['origin'][1], places=3)

    def test_visible_overlap_between_two_translations_is_rejected(self):
        with fitz.open() as scratch:
            first = Block('a', 0, [10, 10, 150, 50], 'Original A', translation='重叠内容')
            second = Block('b', 0, [10, 10, 150, 50], 'Original B', translation='重叠内容')
            a = _text_page(scratch, first, Options())
            b = _text_page(scratch, second, Options())
            self.assertEqual(_conflicting_placements([(first, a, None), (second, b, None)], scratch, None), {'a', 'b'})


if __name__ == "__main__":
    unittest.main()
