"""Packaged offline probes for the five review fixes."""
from __future__ import annotations

import copy
from pathlib import Path
import threading

import pymupdf as fitz

from .extraction import analyze
from .layout import export_pdf
from .layout_upgrade import refresh_layout, EXTRACTION_VERSION
from .models import Options
from .translation import protect, restore


def run_checks(directory):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    source, job = root/'source.pdf', root/'job.json'
    with fitz.open() as doc:
        for _ in range(2):
            p = doc.new_page(width=600, height=300)
            p.insert_text((40,70), 'The result x = (a+b)/c is used in this experiment.', fontsize=12)
            p.insert_text((40,84), 'Additional scientific details follow in the same paragraph.', fontsize=12)
        doc.save(source)
    job.unlink(missing_ok=True)
    options = Options(ocr=False, translate_figures=False, vision_review=False, page_range='1')
    project = analyze(str(source), options, str(job), threading.Event(), lambda _: None)
    b = project.blocks[0]
    checks = {'ordinary_font_formula_vector_object': bool(b.inline and b.inline[0]['text'] == 'x = (a+b)/c')}
    protected, tokens = protect('The result x = (a+b)/c is useful.')
    checks['ordinary_formula_request_is_protected'] = 'x = (a+b)/c' in tokens and restore(protected, tokens) == 'The result x = (a+b)/c is useful.'
    b.translation, b.status, b.vertical = '考虑 [[MATH_0]] 可得结论。', 'edited', True
    project.blocks = [b]
    report = export_pdf(project, root/'vertical.pdf', options)
    with fitz.open(root/'vertical.pdf') as output:
        text = ''.join(output[0].get_text().split())
        checks['vertical_formula_renders_without_marker'] = not report['needs_review'] and 'MATH_' not in text and 'x=(a+b)/c' in text
    saved = copy.deepcopy(project)
    saved.extraction_version = 4
    with fitz.open(source) as doc:
        refresh_layout(saved, doc)
    restored = next(x for x in saved.blocks if x.id == b.id)
    checks['upgrade_retains_manual_orientation_and_translation'] = restored.vertical and restored.translation == b.translation and restored.status == 'edited' and saved.extraction_version == EXTRACTION_VERSION
    for block in project.blocks:
        block.translation, block.status = '已完成 [[MATH_0]] 译文。', 'translated'
    project.usage = {'input_tokens': 100, 'output_tokens': 50, 'requests': 1}
    project.save(job)
    options.page_range = '1-2'
    expanded = analyze(str(source), options, str(job), threading.Event(), lambda _: None)
    checks['page_range_keeps_paid_work_and_usage'] = (any(x.page == 0 and x.translation == '已完成 [[MATH_0]] 译文。' for x in expanded.blocks)
                                                    and any(x.page == 1 for x in expanded.blocks) and expanded.usage == project.usage)
    b.vertical, b.translation, b.status = False, '过长 [[MATH_0]] 译文。' * 2000, 'translated'
    # A single marker is required even for an oversized translation.
    b.translation = '过长译文。' * 2000 + '[[MATH_0]]'
    options.page_range = '1'
    export_pdf(project, root/'long.pdf', options)
    b.translation, b.status = '短译文 [[MATH_0]]。', 'edited'
    report = export_pdf(project, root/'short.pdf', options)
    checks['resolved_overflow_warning_is_removed'] = b.id not in report['needs_review'] and not b.notes
    return checks
