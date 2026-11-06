from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest

import pymupdf as fitz

from papertranslator.extraction import analyze, native_blocks
from papertranslator.layout import export_pdf
from papertranslator.layout_upgrade import refresh_layout, EXTRACTION_VERSION
from papertranslator.math_text import math_values, unchanged_math
from papertranslator.models import Options, Provider
from papertranslator.native_geometry import glyph_key
from papertranslator.translation import protect, restore, translate_project


class OfflineClient:
    def __init__(self, corrupt=False):
        self.provider, self.cancel, self.retrying = Provider(model='offline', vision=False), threading.Event(), False
        self.requests, self.corrupt = [], corrupt

    def complete(self, messages):
        segments = json.loads(messages[-1]['content'])['segments']
        self.requests.extend(s['id'] for s in segments)
        return json.dumps({'segments': [{'id': s['id'], 'translation': '错误 x=(a-b)/c。' if self.corrupt else '译文：' + s['source']} for s in segments]})


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.temp.name)
        self.source, self.job = self.root/'source.pdf', self.root/'job.json'
        self.options = Options(ocr=False, translate_figures=False, vision_review=False)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, formula=False):
        with fitz.open() as doc:
            for n in range(2):
                p = doc.new_page(width=600, height=300)
                if formula:
                    p.insert_text((40, 70), 'The result x = (a+b)/c is used in this experiment.', fontsize=12)
                else:
                    p.insert_text((40, 70), 'The training data contains a complete scientific paragraph.', fontsize=12)
                p.insert_text((40, 84), 'Additional scientific details follow in the same paragraph.', fontsize=12)
            doc.save(self.source)
        return self.analyze()

    def analyze(self, options=None):
        return analyze(str(self.source), options or self.options, str(self.job), threading.Event(), lambda _: None)

    def test_upgrade_preserves_manual_source_orientation_and_keep_original(self):
        original = self.fixture()
        for version in (3, 4):
            for mode in ('source', 'direction', 'keep', 'pending-source'):
                with self.subTest(version=version, mode=mode):
                    project = copy.deepcopy(original)
                    b = project.blocks[0]
                    b.status, b.translation = 'edited', '人工校对的译文。'
                    if mode == 'source' or mode == 'pending-source':
                        b.text = 'MANUALLY CORRECTED scientific paragraph.'
                    if mode == 'pending-source':
                        b.status, b.translation = 'pending', ''
                    if mode == 'direction':
                        b.angle, b.vertical = 90, True
                    if mode == 'keep':
                        b.protected, b.status, b.translation = True, 'preserved', b.text
                    expected = copy.deepcopy(vars(b))
                    project.extraction_version = version
                    with fitz.open(self.source) as doc:
                        refresh_layout(project, doc)
                    actual = next(a for a in project.blocks if a.id == b.id)
                    for field in ('text', 'translation', 'protected', 'angle', 'vertical', 'status'):
                        self.assertEqual(getattr(actual, field), expected[field], field)
                    self.assertEqual(project.extraction_version, EXTRACTION_VERSION)
                    self.assertEqual(len({b.id for b in project.blocks}), len(project.blocks))

    def test_manual_region_survives_new_paragraph_boundaries_without_duplicate_glyphs(self):
        project = self.fixture()
        b = project.blocks[0]
        b.erasers, b.glyphs = b.erasers[:15], b.glyphs[:15]
        b.text, b.translation, b.status = 'Corrected source fragment.', '手工片段。', 'edited'
        project.blocks = [b]
        project.selected_pages = [0]
        project.extraction_version = 4
        with fitz.open(self.source) as doc:
            expected = set().union(*(glyph_key(x) for x in native_blocks(doc[0], 0)))
            refresh_layout(project, doc)
        actual = [p for x in project.blocks for p in glyph_key(x)]
        self.assertEqual(set(actual), expected)
        self.assertEqual(len(actual), len(set(actual)))
        self.assertEqual(next(x for x in project.blocks if x.id == b.id).text, 'Corrected source fragment.')

    def test_page_range_expansion_shrink_and_reexpansion_preserve_work_and_usage(self):
        self.options.page_range = '1'
        project = self.fixture()
        for b in project.blocks:
            b.translation, b.status = '已完成译文。', 'translated'
        project.usage = {'input_tokens': 100, 'output_tokens': 50, 'requests': 1}
        project.provider = Provider(model='offline', vision=False).public_dict()
        project.save(self.job)
        self.options.page_range = '1-2'
        project = self.analyze()
        self.assertTrue(all(b.translation == '已完成译文。' for b in project.blocks if b.page == 0))
        self.assertEqual(project.usage['input_tokens'], 100)
        client = OfflineClient()
        translate_project(project, self.options, client, lambda: None, lambda _: None)
        self.assertTrue(client.requests)
        self.assertTrue(all(i.startswith('p2-') for i in client.requests))
        project.save(self.job)
        saved = {b.id: b.translation for b in project.blocks}
        for spec in ('1', '1,2'):
            self.options.page_range = spec
            project = self.analyze()
            self.assertEqual({b.id: b.translation for b in project.blocks}, saved)
            self.assertTrue(all(u.page in project.selected_pages for u in project.units))
            self.assertEqual(project.usage['input_tokens'], 100)
            project.save(self.job)

    def test_unselected_cached_page_is_not_sent_for_translation(self):
        project = self.fixture()
        project.selected_pages = [0]
        client = OfflineClient()
        translate_project(project, self.options, client, lambda: None, lambda _: None)
        self.assertTrue(all(i.startswith('p1-') for i in client.requests))
        self.assertTrue(all(b.status == 'pending' for b in project.blocks if b.page == 1))

    def test_reanalysis_keeps_matching_translations_manual_edits_and_statistics(self):
        project = self.fixture()
        for b in project.blocks:
            b.translation, b.status = '已完成译文。', 'translated'
        project.blocks[0].text, project.blocks[0].status = 'Corrected source.', 'edited'
        project.usage['input_tokens'] = 120
        project.save(self.job)
        self.options.ocr_dpi = 300
        project = self.analyze()
        self.assertEqual(project.blocks[0].text, 'Corrected source.')
        self.assertTrue(all(b.translation == '已完成译文。' for b in project.blocks))
        self.assertEqual(project.usage['input_tokens'], 120)

    def test_language_change_still_invalidates_automatic_translation(self):
        project = self.fixture()
        for b in project.blocks:
            b.translation, b.status = '旧中文译文。', 'translated'
        project.provider = Provider(model='offline', vision=False).public_dict()
        project.save(self.job)
        changed = copy.deepcopy(self.options)
        changed.target_language = '德语'
        project = self.analyze(changed)
        client = OfflineClient()
        translate_project(project, changed, client, lambda: None, lambda _: None)
        self.assertEqual(len(client.requests), len(project.blocks))

    def test_ordinary_font_formula_is_a_vector_inline_object(self):
        project = self.fixture(formula=True)
        b = project.blocks[0]
        self.assertEqual(b.inline[0]['text'], 'x = (a+b)/c')
        self.assertIn('[[MATH_0]]', b.template)
        client = OfflineClient()
        translate_project(project, self.options, client, lambda: None, lambda _: None)
        report = export_pdf(project, self.root/'out.pdf', self.options)
        self.assertFalse(report['needs_review'])
        with fitz.open(self.root/'out.pdf') as doc:
            self.assertNotIn('MATH_', doc[0].get_text())
            self.assertIn('x = (a+b)/c', doc[0].get_text())
            self.assertTrue(all(i['width'] == 2 for i in doc[0].get_image_info()))

    def test_formula_mutation_from_model_is_rejected_and_saved_manual_mutation_keeps_original(self):
        project = self.fixture(formula=True)
        client = OfflineClient(corrupt=True)
        translate_project(project, self.options, client, lambda: None, lambda _: None)
        self.assertTrue(all(b.status == 'failed' and not b.translation for b in project.blocks if b.inline))
        b = project.blocks[0]
        project.blocks, project.selected_pages = [b], [0]
        b.inline, b.template = [], ''
        b.translation, b.status = '本实验使用 x=(a-b)/c。', 'edited'
        report = export_pdf(project, self.root/'out.pdf', self.options)
        self.assertIn(b.id, report['needs_review'])
        with fitz.open(self.source) as original, fitz.open(self.root/'out.pdf') as output:
            self.assertEqual(original[0].get_pixmap().samples, output[0].get_pixmap().samples)

    def test_text_formula_fallback_and_natural_compounds(self):
        text = 'The result x = (a+b)/c and E = mc^2 are used.'
        value, tokens = protect(text)
        self.assertIn('x = (a+b)/c', tokens)
        self.assertIn('E = mc^2', tokens)
        self.assertEqual(restore(value, tokens), text)
        self.assertEqual(math_values('English-Chinese translation; state-of-the-art method.'), [])
        self.assertEqual(math_values('Learning rate = 0.1; accuracy = 95%.'), [])
        self.assertEqual(math_values('An incomplete (' + 'a'*5000), [])
        self.assertEqual(math_values('研究使用Rosières-Près-Troyes的方法。'), [])
        self.assertTrue(unchanged_math('We use x = a+b in this experiment.', '结果x=a+b用于该实验。'))
        self.assertFalse(unchanged_math('We use x = a+b.', '结果x=a+b+c用于该实验。'))

    def test_vertical_formula_is_kept_whole_without_literal_markers(self):
        project = self.fixture(formula=True)
        b = project.blocks[0]
        project.blocks, project.selected_pages = [b], [0]
        b.vertical, b.translation, b.status = True, '考虑 [[MATH_0]] 可得结论。', 'edited'
        report = export_pdf(project, self.root/'vertical.pdf', self.options)
        self.assertFalse(report['needs_review'])
        with fitz.open(self.root/'vertical.pdf') as doc:
            text = ''.join(doc[0].get_text().split())
            self.assertNotIn('MATH_', text)
            self.assertIn('x=(a+b)/c', text)
            self.assertTrue(all(c in text for c in '考虑可得结论'))

    def test_missing_vertical_formula_marker_retains_original(self):
        project = self.fixture(formula=True)
        b = project.blocks[0]
        project.blocks, project.selected_pages = [b], [0]
        b.vertical, b.translation, b.status = True, '考虑可得结论。', 'edited'
        report = export_pdf(project, self.root/'vertical.pdf', self.options)
        self.assertIn(b.id, report['needs_review'])
        with fitz.open(self.source) as original, fitz.open(self.root/'vertical.pdf') as output:
            self.assertEqual(original[0].get_pixmap().samples, output[0].get_pixmap().samples)

    def test_corrected_overflow_clears_old_layout_warning_but_keeps_ocr_notes(self):
        project = self.fixture()
        b = project.blocks[0]
        project.blocks, project.selected_pages = [b], [0]
        b.translation, b.status = '过长译文。'*2000, 'translated'
        self.assertIn(b.id, export_pdf(project, self.root/'long.pdf', self.options)['needs_review'])
        b.translation, b.status = '完整短译文。', 'edited'
        report = export_pdf(project, self.root/'short.pdf', self.options)
        self.assertNotIn(b.id, report['needs_review'])
        self.assertFalse(b.notes)
        b.note('OCR 置信度 88%，请核对原文。')
        report = export_pdf(project, self.root/'review.pdf', self.options)
        self.assertIn(b.id, report['needs_review'])
        self.assertEqual(b.notes, ['OCR 置信度 88%，请核对原文。'])


if __name__ == '__main__':
    unittest.main()
