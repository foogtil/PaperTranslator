"""Character-level filtering and algebra tagging before paragraph grouping."""
from __future__ import annotations

import pymupdf as fitz
from .math_text import math_ranges


def center_key(rect):
    x0, y0, x1, y1 = rect
    return round((x0 + x1) / 2, 3), round((y0 + y1) / 2, 3)


def glyph_key(block):
    return frozenset(center_key(r) for r in block.erasers)


def _bounds(rects):
    result = fitz.Rect(rects[0])
    for rect in rects[1:]:
        result |= fitz.Rect(rect)
    return tuple(result)


def prepare_spans(data, excluded=()):
    ignored = {point for b in excluded for point in glyph_key(b)}
    fallback = [fitz.Rect(b.rect) for b in excluded if not b.erasers]
    kept_blocks = []
    for raw in data['blocks']:
        if raw.get('type') != 0:
            kept_blocks.append(raw)
            continue
        lines = []
        for line in raw['lines']:
            spans = []
            for span in line['spans']:
                chars = [c for c in span['chars'] if center_key(c['bbox']) not in ignored
                         and not any(r.contains(fitz.Point(center_key(c['bbox']))) for r in fallback)]
                if chars:
                    spans.append({**span, 'chars': chars, 'bbox': _bounds([c['bbox'] for c in chars])})
            # One formula may use several ordinary-font spans. Tag by position
            # across the visual PDF line, retaining original font and origins.
            text = ''.join(c['c'] for s in spans for c in s['chars'])
            mixed_math = any(s.get('flags', 0) & (1 | 2 | 8) or any(n in s.get('font', '').lower() for n in ('math', 'symbol', 'cmmi', 'cmsy', 'cmex')) for s in spans)
            ranges = [] if mixed_math else list(math_ranges(text))
            offset, split = 0, []
            for span in spans:
                group, previous = [], None
                for char in span['chars']:
                    hard = any(a <= offset < b for a, b in ranges)
                    if group and hard != previous:
                        split.append({**span, 'chars': group, 'bbox': _bounds([c['bbox'] for c in group]), 'math_literal': previous})
                        group = []
                    group.append(char)
                    previous = hard
                    offset += len(char['c'])
                if group:
                    split.append({**span, 'chars': group, 'bbox': _bounds([c['bbox'] for c in group]), 'math_literal': previous})
            if split:
                lines.append({**line, 'spans': split, 'bbox': _bounds([s['bbox'] for s in split])})
        if lines:
            kept_blocks.append({**raw, 'lines': lines, 'bbox': _bounds([l['bbox'] for l in lines])})
    data['blocks'] = kept_blocks
