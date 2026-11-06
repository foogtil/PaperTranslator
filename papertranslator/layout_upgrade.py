"""Reconcile source geometry without discarding paid work or manual edits."""
from __future__ import annotations

from collections import defaultdict
import re

import pymupdf as fitz

from .models import checkpoint
from .native_geometry import glyph_key, center_key

EXTRACTION_VERSION = 6
NATIVE_ID = re.compile(r"p\d+-n\d+")
MARKER = re.compile(r'\[\[MATH_\d+\]\]')


def retarget_translation(previous, fresh):
    """Reuse a translation when the same source glyphs gain/lose math tags."""
    if not previous.translation.strip() or re.sub(r'\s+', '', previous.text) != re.sub(r'\s+', '', fresh.text):
        return None
    expanded = previous.template or previous.text
    for item in previous.inline:
        if expanded.count(item['marker']) != 1:
            return None
        expanded = expanded.replace(item['marker'], item['text'], 1)
    if re.sub(r'\s+', '', expanded) != re.sub(r'\s+', '', previous.text):
        return None
    value = previous.translation
    for item in previous.inline:
        if value.count(item['marker']) != 1:
            return None
        value = value.replace(item['marker'], item['text'], 1)
    for item in fresh.inline:
        if value.count(item['text']) != 1:
            return None
        value = value.replace(item['text'], item['marker'], 1)
    if sorted(MARKER.findall(value)) != sorted(i['marker'] for i in fresh.inline):
        return None
    return value


def _key(block):
    return (block.kind, re.sub(r"\s+", " ", block.text).strip(),
            re.sub(r"\s+", " ", block.template or block.text).strip(),
            tuple((i["marker"], tuple(round(v, 2) for v in i["rect"])) for i in block.inline),
            glyph_key(block) if block.erasers else tuple(round(v, 2) for v in block.rect))


def _manual(previous, baseline=None):
    if previous.status == 'edited' or previous.approved:
        return True
    if baseline is None:
        return previous.protected and bool(re.search(r'[a-z]{3,}|[\u3400-\u9fff]{2,}', previous.text))
    return (previous.protected != baseline.protected or previous.angle != baseline.angle
            or previous.vertical != baseline.vertical
            or (previous.text != baseline.text and previous.status in ('pending', 'failed')))


def native_with_manual(page, index, figures, old):
    """Manual native regions are opaque anchors; re-extract the other glyphs."""
    from .extraction import native_blocks
    fresh = native_blocks(page, index, figures)
    by_geometry = {glyph_key(b): b for b in fresh if b.erasers}
    pinned = [b for b in old if NATIVE_ID.fullmatch(b.id)
              and _manual(b, by_geometry.get(glyph_key(b)) if b.erasers else None)]
    if not pinned:
        return fresh
    lookup = {center_key(c['bbox']): list(c['bbox']) for raw in page.get_text('rawdict')['blocks'] if raw.get('type') == 0
              for line in raw['lines'] for span in line['spans'] for c in span['chars'] if not c['c'].isspace()}
    for b in pinned:
        if not b.glyphs:
            b.glyphs = [lookup[p] for p in glyph_key(b) if p in lookup]
    fresh = native_blocks(page, index, figures, excluded=pinned)
    return fresh + pinned


def reconcile_blocks(old, fresh):
    """Reuse exact matches; retain incompatible automatic translations as drafts."""
    by_key = defaultdict(list)
    by_geometry = defaultdict(list)
    for b in old:
        by_key[_key(b)].append(b)
        if b.erasers:
            by_geometry[glyph_key(b)].append(b)
    reused, stable = set(), set()
    for b in fresh:
        retargeted = {}
        matches = [p for p in by_key[_key(b)] if id(p) not in reused]
        if not matches and b.erasers and not b.protected:
            for p in by_geometry[glyph_key(b)]:
                if id(p) in reused:
                    continue
                value = retarget_translation(p, b)
                if value is not None:
                    matches = [p]
                    retargeted[id(p)] = value
                    break
        if not matches:
            continue
        p = min(matches, key=lambda p: sum(abs(a-v) for a, v in zip(b.rect, p.rect)))
        reused.add(id(p))
        stable.add(id(b))
        if b is p or b.protected:
            continue
        b.id = p.id
        b.translation, b.status, b.approved, b.layout_stale = p.translation, p.status, p.approved, p.layout_stale
        if id(p) in retargeted:
            b.translation = retargeted[id(p)]
        b.notes = list(dict.fromkeys(b.notes + p.notes))
    for b in fresh:
        if b.protected or id(b) in stable or b.translation:
            continue
        candidates = [p for p in old if id(p) not in reused and not p.protected and p.translation.strip()
                      and (fitz.Rect(p.rect) & fitz.Rect(b.rect)).get_area() > .3 * min(fitz.Rect(p.rect).get_area(), fitz.Rect(b.rect).get_area())]
        if candidates:
            candidates.sort(key=lambda p: (p.rect[1], p.rect[0]))
            b.translation = '\n'.join(p.translation for p in candidates)
            b.status, b.layout_stale = 'layout_pending', True
            b.note('公式或段落结构已更新；旧译文保留供核对，请重新翻译或人工确认后导出。')
    old_objects = {id(b) for b in old}
    reserved = {b.id for b in fresh if id(b) in stable or id(b) in old_objects}
    used = set()
    for b in fresh:
        if b.id in used or (b.id in reserved and id(b) not in stable and id(b) not in old_objects):
            prefix, n = f'p{b.page+1}-' + ('n' if b.erasers or b.kind == 'native' else 'o'), 1
            while prefix + str(n) in used | reserved:
                n += 1
            b.id = prefix + str(n)
        used.add(b.id)
    return fresh


def refresh_layout(project, document, cancel=None, log=None):
    if project.extraction_version not in (3, 4, 5):
        return
    from .structure import assign_units, figure_rects
    log = log or (lambda value: None)
    figures_by_page = {}
    for index, info in enumerate(project.pages):
        checkpoint(cancel)
        if not info.analyzed:
            continue
        old = [b for b in project.blocks if b.page == index and NATIVE_ID.fullmatch(b.id)]
        page = document[index]
        rotation = page.rotation
        page.set_rotation(0)
        try:
            figures = figure_rects(page)
            fresh = reconcile_blocks(old, native_with_manual(page, index, figures, old)) if old else []
        finally:
            page.set_rotation(rotation)
        if old:
            old_ids = {b.id for b in old}
            project.blocks = [b for b in project.blocks if b.page != index or b.id not in old_ids] + fresh
        figures_by_page[index] = [list(r) for r in figures]
    assign_units(project, figures_by_page)
    project.blocks.sort(key=lambda b: (b.page, b.rect[1], b.rect[0]))
    project.extraction_version = EXTRACTION_VERSION
    log('已更新公式与表格结构；人工修改、可复用译文及用量统计已保留。')
