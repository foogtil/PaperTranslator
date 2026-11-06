from pathlib import Path
import copy
import tempfile
import threading
import unittest
from unittest.mock import patch

import pymupdf as fitz

from papertranslator.disease_checks import table_fixture, raster_edge_check
from papertranslator.extraction import analyze
from papertranslator.layout import export_pdf, _add_redaction_quads
from papertranslator.layout_upgrade import refresh_layout, EXTRACTION_VERSION, reconcile_blocks
from papertranslator.math_text import math_values
from papertranslator.models import Options, Block
from papertranslator.native_geometry import center_key
from papertranslator.redaction import characters, RectIndex


class DiseaseRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root=Path(self.temp.name)
        self.source=self.root/'source.pdf'
        self.options=Options(ocr=False,translate_figures=False,vision_review=False)

    def tearDown(self):
        self.temp.cleanup()

    def analyze(self):
        return analyze(str(self.source),self.options,str(self.root/'job.json'),threading.Event(),lambda _:None)

    def metric_fixture(self, angle=0, crop=False):
        with fitz.open('assets/selftest/metric-font.pdf') as d:
            if angle:
                font=d.extract_font(d[0].get_fonts()[0][0])[3]
                with fitz.open() as rotated:
                    p=rotated.new_page(width=400,height=400)
                    p.insert_font(fontname='probe',fontbuffer=font)
                    p.insert_text((200,200),'ABC ABC',fontname='probe',fontsize=20,rotate=angle)
                    if crop:p.set_cropbox(fitz.Rect(30,20,350,370))
                    rotated.save(self.source)
            else:
                if crop:d[0].set_cropbox(fitz.Rect(20,20,500,700))
                d.save(self.source)

    def test_short_ascent_fixture_reproduces_center_eraser_failure(self):
        self.metric_fixture()
        project=self.analyze()
        with fitz.open(self.source) as doc:
            _add_redaction_quads(doc[0],[r for b in project.blocks for r in b.erasers])
            doc[0].apply_redactions(images=0,graphics=0,text=0)
            self.assertIn('ABC',doc[0].get_text())

    def test_baseline_erasers_remove_nonstandard_font_for_all_page_directions(self):
        for angle,crop in ((0,False),(0,True),(90,False),(180,False),(270,True)):
            with self.subTest(angle=angle,crop=crop):
                (self.root/'job.json').unlink(missing_ok=True)
                self.metric_fixture(angle,crop)
                project=self.analyze()
                for b in project.blocks:b.translation,b.status='译文','translated'
                report=export_pdf(project,self.root/'out.pdf',self.options)
                self.assertFalse(report['needs_review'],report['regions'])
                with fitz.open(self.root/'out.pdf') as doc:
                    self.assertNotIn('ABC',doc[0].get_text())
                    self.assertIn('译文',doc[0].get_text())

    def test_failed_deletion_is_detected_before_overlay_and_keeps_source(self):
        self.metric_fixture()
        project=self.analyze()
        for b in project.blocks:b.translation,b.status='不得叠印','translated'
        def broken(b,chars):return b.erasers,{center_key(r) for r in b.erasers}
        with patch('papertranslator.redaction.erasers_for',side_effect=broken):
            report=export_pdf(project,self.root/'out.pdf',self.options)
        self.assertEqual(report['translated_regions'],0)
        self.assertTrue(report['needs_review'])
        with fitz.open(self.source) as src,fitz.open(self.root/'out.pdf') as out:
            self.assertIn('ABC',out[0].get_text())
            self.assertNotIn('不得叠印',out[0].get_text())
            self.assertEqual(src[0].get_pixmap().samples,out[0].get_pixmap().samples)

    def test_unintended_deletion_of_neighbor_is_rejected(self):
        self.metric_fixture()
        project=self.analyze()
        b=project.blocks[0]
        # Target only the first three glyphs while an injected faulty eraser
        # would remove the whole line, including its protected neighbour.
        b.erasers,b.glyphs=b.erasers[:3],b.glyphs[:3]
        b.translation,b.status='不得叠印','translated'
        def broken(b,chars):return [[0,0,300,100]],{center_key(r) for r in b.erasers}
        with patch('papertranslator.redaction.erasers_for',side_effect=broken):
            report=export_pdf(project,self.root/'out.pdf',self.options)
        self.assertEqual(report['translated_regions'],0)
        with fitz.open(self.root/'out.pdf') as doc:self.assertEqual(doc[0].get_text().count('ABC'),2)

    def test_short_ascent_neighbor_survives_at_original_position(self):
        self.metric_fixture()
        project=self.analyze()
        b=project.blocks[0]
        with fitz.open(self.source) as src:
            all_chars=characters(src[0])
            rect=fitz.Rect(all_chars[0][1]['bbox'])
            for _,c,_,_ in all_chars[1:3]:rect|=fitz.Rect(c['bbox'])
            neighbor=fitz.Rect(all_chars[3][1]['bbox'])
            for _,c,_,_ in all_chars[4:]:neighbor|=fitz.Rect(c['bbox'])
        b.erasers,b.glyphs=b.erasers[:3],b.glyphs[:3]
        b.rect=list(rect);b.local_width,b.local_height=rect.width,rect.height
        b.text,b.translation,b.status='ABC','译文','translated'
        report=export_pdf(project,self.root/'out.pdf',self.options)
        self.assertFalse(report['needs_review'],report['regions'])
        with fitz.open(self.source) as src,fitz.open(self.root/'out.pdf') as out:
            self.assertEqual(out[0].get_text().count('ABC'),1)
            self.assertEqual(src[0].get_pixmap(clip=neighbor,dpi=144).samples,out[0].get_pixmap(clip=neighbor,dpi=144).samples)

    def test_table_cells_preserve_numbers_and_do_not_absorb_other_column(self):
        table_fixture(self.source)
        project=self.analyze()
        cells=[b for b in project.blocks if b.role=='table_cell']
        self.assertEqual(len(cells),12,[(b.text,b.rect) for b in cells])
        numeric=[b for b in cells if b.protected]
        self.assertEqual(len(numeric),6)
        self.assertTrue(all(b.rect[2]<350 for b in cells))
        self.assertTrue(any('neighbouring' in b.text and b.role!='table_cell' for b in project.blocks))
        for b in project.blocks:
            if not b.protected:b.translation,b.status='指标','translated'
        report=export_pdf(project,self.root/'out.pdf',self.options)
        self.assertFalse(report['needs_review'],report['regions'])
        with fitz.open(self.source) as src,fitz.open(self.root/'out.pdf') as out:
            for b in numeric:
                r=fitz.Rect(b.glyphs[0])
                for box in b.glyphs[1:]:r|=fitz.Rect(box)
                self.assertEqual(src[0].get_pixmap(clip=r,dpi=144).samples,out[0].get_pixmap(clip=r,dpi=144).samples)

    def test_table_upgrade_marks_incompatible_prose_as_draft_and_keeps_paid_work(self):
        table_fixture(self.source)
        project=self.analyze()
        cells=[b for b in project.blocks if b.role=='table_cell']
        old=copy.deepcopy(cells[0]);old.id='p1-n999';old.role='paragraph'
        old.text=' '.join(b.text for b in cells)
        old.erasers=[e for b in cells for e in b.erasers]
        old.glyphs=[e for b in cells for e in b.glyphs]
        r=fitz.Rect(cells[0].rect)
        for b in cells[1:]:r|=fitz.Rect(b.rect)
        old.rect=list(r);old.translation='旧版合并表格译文。';old.status='translated'
        prose=[b for b in project.blocks if b.role!='table_cell']
        for b in prose:b.translation,b.status='已完成的相邻正文。','translated'
        project.blocks=prose+[old];project.extraction_version=5
        project.usage['input_tokens']=777
        with fitz.open(self.source) as doc:refresh_layout(project,doc)
        self.assertEqual(project.extraction_version,EXTRACTION_VERSION)
        self.assertEqual(project.usage['input_tokens'],777)
        self.assertTrue(all(b.translation=='已完成的相邻正文。' for b in project.blocks if b.role!='table_cell'))
        self.assertTrue(all(b.layout_stale for b in project.blocks if b.role=='table_cell' and not b.protected))
        report=export_pdf(project,self.root/'draft.pdf',self.options)
        self.assertTrue(report['needs_review'])

    def test_identifiers_are_not_split_into_plain_font_math_objects(self):
        for value in ('https://doi.org/10.1016/j.eswa.2026.133728','10.1016/j.eswa.2026.133728','a-b@example.org'):
            self.assertEqual(math_values(value),[],value)
        self.assertEqual(math_values('The value x = (a+b)/c is retained.'),['x = (a+b)/c'])

    def test_doi_tag_upgrade_reuses_translation_without_changing_its_visible_identifier(self):
        source='See https://doi.org/10.1016/j.eswa.2026.133728 for details.'
        old=Block('p1-n1',0,[20,20,400,60],source,erasers=[[30,30,30.05,30.05]],
                  template=source.replace('org/10.1016/j','[[MATH_0]]'),
                  inline=[{'marker':'[[MATH_0]]','text':'org/10.1016/j','rect':[50,20,100,30]}],
                  translation='详情参见 https://doi.[[MATH_0]].eswa.2026.133728。',status='translated')
        fresh=Block('p1-n2',0,[20,20,400,60],source,erasers=copy.deepcopy(old.erasers))
        result=reconcile_blocks([old],[fresh])[0]
        self.assertEqual(result.translation,'详情参见 https://doi.org/10.1016/j.eswa.2026.133728。')
        self.assertFalse(result.layout_stale)

    def test_short_title_tail_is_one_translation_paragraph_even_beside_publisher_icon(self):
        from papertranslator.extraction import native_blocks
        with fitz.open() as doc:
            p=doc.new_page(width=650,height=300)
            p.insert_text((40,70),'Heterogeneous transfer learning for robust disease forecasting: A',fontsize=14)
            p.insert_text((40,90),'data-centric approach',fontsize=14)
            blocks=native_blocks(p,0,[fitz.Rect(20,20,620,74)])
        self.assertEqual(len(blocks),1)
        self.assertTrue(blocks[0].text.endswith('A data-centric approach'))

    def test_dense_cell_does_not_inherit_library_body_margins(self):
        from papertranslator.layout import _text_page
        b=Block('dense',0,[0,0,30,7.33],'Variable',role='table_cell',fontsize=6.376,
                translation='变量',status='translated')
        with fitz.open() as scratch:
            placement=_text_page(scratch,b,self.options)
            self.assertIsNotNone(placement)
            self.assertGreaterEqual(b.fontsize*placement[2],6)
            self.assertIn('变量',scratch[placement[0]].get_text())

    def test_protected_glyph_index_matches_exact_geometry_at_cell_boundaries(self):
        rects=[fitz.Rect(x,y,x+12,y+9) for x in range(-50,200,9) for y in range(-35,180,11)]
        index=RectIndex(rects)
        for x,y in ((0,0),(32,32),(-32,-32),(64,64),(256,300),(31.99,9),(199,199)):
            r=fitz.Rect(x-.025,y-.025,x+.025,y+.025)
            self.assertEqual(index.intersects(r),any(p.intersects(r) for p in rects))

    def test_raster_erasure_has_no_gray_source_outline_at_high_zoom_and_keeps_axis(self):
        self.assertTrue(raster_edge_check())


if __name__=='__main__':unittest.main()
