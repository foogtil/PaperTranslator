"""Offline geometry probes also executed inside the packaged EXE."""
from __future__ import annotations

import copy
from pathlib import Path
import threading

import pymupdf as fitz

from .extraction import analyze, native_blocks
from .layout import _text_page, export_pdf
from .models import Options


def run_checks(directory):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    checks = {}
    with fitz.open() as doc, fitz.open() as scratch:
        p = doc.new_page(width=650, height=250)
        for point, text, font, size in [((40, 60), 'We calculate residuals using', 'helv', 11),
                                        ((180, 60), 'Y', 'tiit', 11), ((182, 57), '^', 'helv', 9),
                                        ((188, 62), 't', 'tiit', 7), ((191, 55), 'MODEL', 'helv', 7),
                                        ((222, 60), 'for all samples in the training set and all future values.', 'helv', 11)]:
            p.insert_text(point, text, fontname=font, fontsize=size)
        blocks = native_blocks(p, 0)
        b = next(b for b in blocks if 'residuals' in b.text)
        checks['formula_hat_and_scripts_stay_together'] = (len(b.inline) == 1 and 'MODEL' in b.inline[0]['text']
                                                          and '^' in b.inline[0]['text'] and 'for all' not in b.inline[0]['text'])
        b.translation = '由公式 [[MATH_0]] 计算全部训练样本和未来观测的残差。'
        placed = _text_page(scratch, b, Options(), doc)
        geometry = False
        if placed:
            page = scratch[placed[0]]
            image = fitz.Rect(page.get_image_info()[0]['bbox'])
            original = fitz.Rect(b.inline[0]['rect'])
            spans = [s for raw in page.get_text('dict')['blocks'] if raw.get('type') == 0 for line in raw['lines'] for s in line['spans']]
            base = next(s for s in spans if 'Times-Italic' in s['font'] and 'Y' in s['text'])
            prose = next(s for s in spans if '公式' in s['text'])
            geometry = (abs(image.width / original.width - image.height / original.height) < .0001
                        and abs(base['origin'][1] - prose['origin'][1]) < .01
                        and all(i['width'] == i['height'] == 2 for i in page.get_image_info()))
        checks['formula_fractional_dimensions_and_baseline'] = geometry
    source = root / 'wrapped-source.pdf'
    photo = fitz.Rect(30, 40, 105, 130)
    with fitz.open() as doc:
        p = doc.new_page(width=340, height=300)
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 50, 60), False)
        pix.clear_with(80)
        p.insert_image(photo, stream=pix.tobytes('png'))
        for y in (55, 70, 85, 100, 115):
            p.insert_text((115, y), 'Biography beside the photograph.', fontsize=9)
        p.insert_text((30, 145), 'The same paragraph continues below the photograph.', fontsize=9)
        p.insert_text((30, 160), 'Its complete text must remain in the translated PDF.', fontsize=9)
        p.set_rotation(90)
        doc.save(source)
    options = Options(ocr=False, translate_figures=False, vision_review=False)
    job = root / 'wrapped-job.json'
    job.unlink(missing_ok=True)
    project = analyze(str(source), options, str(job), threading.Event(), lambda _: None)
    prose = [b for b in project.blocks if not b.protected]
    b = copy.deepcopy(prose[0])
    rect = fitz.Rect(b.rect)
    for other in prose[1:]:
        rect |= fitz.Rect(other.rect)
        b.erasers.extend(other.erasers)
        b.glyphs.extend(other.glyphs)
    b.rect = list(rect)
    b.translation, b.status = '这是一段围绕照片排版的完整作者介绍。' * 12 + '完整结尾。', 'translated'
    project.blocks = [b]
    output = root / 'wrapped-output.pdf'
    report = export_pdf(project, output, options)
    with fitz.open(source) as original, fitz.open(output) as result:
        same_rotation = original[0].rotation == result[0].rotation == 90
        original[0].set_rotation(0)
        result[0].set_rotation(0)
        checks['wrapped_photo_pixels_preserved'] = same_rotation and (original[0].get_pixmap(clip=photo, dpi=144).samples == result[0].get_pixmap(clip=photo, dpi=144).samples)
        checks['wrapped_translation_complete'] = not report['needs_review'] and ''.join(result[0].get_text().split()) == ''.join(b.translation.split())
    for name, text, stale in [('stale', '旧结构的译文', True), ('overflow', '放不下的译文' * 1000, False)]:
        b.translation, b.status, b.layout_stale = text, 'failed' if stale else 'translated', stale
        path = root / (name + '-output.pdf')
        report = export_pdf(project, path, options)
        with fitz.open(source) as original, fitz.open(path) as result:
            checks[name + '_retains_original'] = b.id in report['needs_review'] and original[0].get_pixmap().samples == result[0].get_pixmap().samples
    return checks
