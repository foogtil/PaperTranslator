"""Place source erasers from baselines and verify removal before overlaying text."""
from __future__ import annotations

import pymupdf as fitz
from collections import defaultdict
from math import floor

from .models import checkpoint
from .native_geometry import center_key


class RectIndex:
    """Limit exact collision checks to nearby protected glyphs in dense tables."""
    def __init__(self, rectangles):
        self.cells = defaultdict(list)
        for value in rectangles:
            rect = fitz.Rect(value)
            for cell in self._cells(rect):
                self.cells[cell].append(rect)

    @staticmethod
    def _cells(rect):
        return ((x, y) for x in range(floor(rect.x0 / 32), floor(rect.x1 / 32) + 1)
                for y in range(floor(rect.y0 / 32), floor(rect.y1 / 32) + 1))

    def intersects(self, value):
        rect = fitz.Rect(value)
        return any(other.intersects(rect) for cell in self._cells(rect) for other in self.cells.get(cell, ()))


def characters(page):
    data = page.get_text('rawdict', flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)
    return [(center_key(c['bbox']), c, s, line['dir']) for raw in data['blocks'] if raw.get('type') == 0
            for line in raw['lines'] for s in line['spans'] for c in s['chars'] if not c['c'].isspace()]


def erasers_for(block, chars):
    """A normalized font bbox center can be outside MuPDF's removal bbox.

    Use a point just above the actual baseline, in the local text direction.
    Keep cached erasers unchanged: their centers identify the original glyphs.
    """
    if not block.erasers:
        rect = fitz.Rect(block.rect)
        return [block.rect], {key for key, c, _, _ in chars if rect.contains(fitz.Point(key))}
    by_key = {key: (c, span, direction) for key, c, span, direction in chars}
    result, targets = [], set()
    for old in block.erasers:
        key = center_key(old)
        entry = by_key.get(key)
        if entry is None:
            result.append(old)
            continue
        c, span, (dx, dy) = entry
        cx, cy = center_key(c['bbox'])
        along = cx * dx + cy * dy
        baseline = -c['origin'][0] * dy + c['origin'][1] * dx
        down = baseline - .15 * span['size']
        x, y = along * dx - down * dy, along * dy + down * dx
        result.append([x - .025, y - .025, x + .025, y + .025])
        targets.add(key)
    return result, targets


def deletion_failures(document, index, plans, add_quads, cancel=None):
    """Check on a page copy: no surviving target or unintended removed glyph."""
    checkpoint(cancel)
    expected = set().union(*(targets for _, _, targets in plans))
    original = {key for key, _, _, _ in characters(document[index])}
    with fitz.open() as probe:
        probe.insert_pdf(document, from_page=index, to_page=index, links=False, annots=False)
        p = probe[0]
        p.set_rotation(0)
        add_quads(p, [r for _, rects, _ in plans for r in rects], cancel)
        p.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                           text=fitz.PDF_REDACT_TEXT_REMOVE)
        remaining = {key for key, _, _, _ in characters(p)}
    if original - remaining - expected:
        # Failing closed also covers coincident source glyphs / unusual fonts.
        return {b.id for b, _, _ in plans}
    return {b.id for b, _, targets in plans if (not targets and b.erasers) or targets & remaining}
