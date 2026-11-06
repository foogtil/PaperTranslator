"""Offline probes for font metrics, deletion verification and tabular positions."""
from __future__ import annotations

from pathlib import Path
import shutil
import sys
import threading

import pymupdf as fitz

from .extraction import analyze
from .layout import export_pdf, _raster_patch
from .math_text import math_values
from .models import Options, Block


def table_fixture(path):
    with fitz.open() as doc:
        p = doc.new_page(width=800, height=300)
        for row, values in enumerate([('Variable','Mean','Max'), ('Daily cases','12.5','99'),
                                       ('Total deaths','3.5','27'), ('Population','1000','2000')]):
            y = 70 + row * 22
            for x, text in zip((40,180,250), values):
                p.insert_text((x,y),text,fontsize=10)
            p.insert_text((360,y),'The neighbouring scientific prose remains a full paragraph.',fontsize=10)
        doc.save(path)


def raster_edge_check():
    with fitz.open() as source, fitz.open() as scanned:
        p=source.new_page(width=200,height=80)
        p.insert_text((15,30),'Training accuracy',fontsize=16,fontname='hebo')
        p.draw_line((10,55),(190,55),width=.8)
        image=p.get_pixmap(dpi=400).tobytes('png')
        p=scanned.new_page(width=200,height=80)
        p.insert_image(p.rect,stream=image)
        block=Block('raster',0,[12,10,170,38],'Training accuracy',kind='raster')
        patch,reason=_raster_patch(p,block,False)
        if reason or patch is None:return False
        axis=fitz.Rect(5,50,195,60)
        before=p.get_pixmap(clip=axis,dpi=600).samples
        p.insert_image(patch[0],stream=patch[1],mask=patch[2],overlay=True)
        return (min(p.get_pixmap(clip=patch[0],dpi=600).samples)>=245
                and p.get_pixmap(clip=axis,dpi=600).samples==before)


def run_checks(directory):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    fixture = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])) / 'assets'/'selftest'/'metric-font.pdf'
    source, job = root/'metrics.pdf', root/'metrics.json'
    shutil.copy2(fixture, source)
    job.unlink(missing_ok=True)
    options = Options(ocr=False, translate_figures=False, vision_review=False)
    project = analyze(str(source),options,str(job),threading.Event(),lambda _:None)
    for b in project.blocks:
        b.translation,b.status = '已验证的译文','translated'
    report = export_pdf(project,root/'metrics-translated.pdf',options)
    with fitz.open(root/'metrics-translated.pdf') as doc:
        text=doc[0].get_text()
        metric_ok=not report['needs_review'] and 'ABC' not in text and '已验证' in text
    table_fixture(root/'table.pdf')
    table_job=root/'table.json'
    table_job.unlink(missing_ok=True)
    table=analyze(str(root/'table.pdf'),options,str(table_job),threading.Event(),lambda _:None)
    cells=[b for b in table.blocks if b.role=='table_cell']
    return {'short_ascent_source_removed_before_translation':metric_ok,
            'tabular_cells_keep_numeric_coordinates':len(cells)==12 and sum(b.protected for b in cells)==6,
            'table_keeps_adjacent_prose_separate':all(b.rect[2]<350 for b in cells) and any('neighbouring' in b.text and b.role!='table_cell' for b in table.blocks),
            'doi_is_not_an_algebra_object':not math_values('https://doi.org/10.1016/j.eswa.2026.133728'),
            'raster_text_erasure_has_no_high_zoom_gray_outlines':raster_edge_check()}
