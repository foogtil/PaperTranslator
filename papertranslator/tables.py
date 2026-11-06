"""Keep aligned tabular cells out of flowing prose paragraphs."""
from __future__ import annotations

import re
import statistics

import pymupdf as fitz


def bounds(spans):
    r = fitz.Rect(spans[0]['bbox'])
    for s in spans[1:]:
        r |= fitz.Rect(s['bbox'])
    return r


def separate_tables(data):
    runs = [(line, s) for raw in data['blocks'] if raw.get('type') == 0
            for line in raw['lines'] if abs(line['dir'][0] - 1) < .01 and not line.get('wmode')
            for s in line['spans'] if any(not c['c'].isspace() for c in s['chars'])]
    rows = []
    for line, span in sorted(runs, key=lambda item: item[1]['origin'][1]):
        baseline, size = span['origin'][1], span['size']
        if not rows or baseline - rows[-1]['baseline'] > min(size, rows[-1]['size']) * .45:
            rows.append({'baseline': baseline, 'size': size, 'runs': []})
        rows[-1]['runs'].append((line, span))
    for row in rows:
        cells = []
        for run in sorted(row['runs'], key=lambda item: item[1]['bbox'][0]):
            if not cells or run[1]['bbox'][0] - bounds([s for _, s in cells[-1]]).x1 > row['size'] * 1.5:
                cells.append([])
            cells[-1].append(run)
        row['cells'] = cells
        row['boxes'] = [bounds([s for _, s in cell]) for cell in cells]
        texts = [''.join(c['c'] for _, s in cell for c in s['chars']).strip() for cell in cells]
        numeric = [n for n, t in enumerate(texts) if re.fullmatch(r'[\d\s.,+−/%()±*∗†‡-]+', t)]
        row['numeric'] = len(numeric)
        if len(numeric) >= 2:
            # A table in one column may share baselines with full prose in
            # the other column. Only its numeric band and row label belong.
            start, stop = max(0, numeric[0]-1), numeric[-1]+1
            row['cells'], row['boxes'] = cells[start:stop], row['boxes'][start:stop]

    def aligned(a, b):
        tolerance = min(a['size'], b['size']) * .75
        return sum(any(min(abs(x.x0-y.x0), abs(x.x1-y.x1)) < tolerance for y in b['boxes']) for x in a['boxes']) >= 3

    candidates = [i for i, r in enumerate(rows) if len(r['cells']) >= 3 and r['numeric'] >= 2]
    selected = set()
    for i, j in zip(candidates, candidates[1:]):
        a, b = rows[i], rows[j]
        if b['baseline'] - a['baseline'] < max(a['size'], b['size']) * 3.5 and aligned(a, b):
            selected.update((i, j))
    # Include the immediately preceding header only when its columns align.
    for i in sorted(selected):
        if i and i-1 not in selected:
            a, b = rows[i-1], rows[i]
            if len(a['cells']) >= 3 and b['baseline'] - a['baseline'] < max(a['size'], b['size']) * 3.5 and aligned(a, b):
                left, right = b['boxes'][0].x0 - b['size'], b['boxes'][-1].x1 + b['size'] * 2
                pairs = [(cell, box) for cell, box in zip(a['cells'], a['boxes']) if left <= box.x0 <= right]
                a['cells'], a['boxes'] = [c for c, _ in pairs], [r for _, r in pairs]
                selected.add(i-1)
    if not selected:
        return
    taken, added = set(), []
    for i in sorted(selected):
        row = rows[i]
        for n, cell in enumerate(row['cells']):
            spans = [s for _, s in cell]
            taken.update(id(s) for s in spans)
            rect = bounds(spans)
            frame = fitz.Rect(rect)
            if n + 1 < len(row['cells']):
                frame.x1 = max(frame.x1, row['boxes'][n+1].x0 - 1.5)
            else:
                # The last header may be shorter than its numeric column.
                # Reuse that column's measured extent, not an arbitrary width.
                peers = [rows[j] for j in selected if len(rows[j]['cells']) == len(row['cells'])
                         and abs(rows[j]['boxes'][0].x0-row['boxes'][0].x0) < row['size']]
                frame.x1 = max([frame.x1] + [r['boxes'][-1].x1 for r in peers])
            frame.y1 = max(frame.y1, frame.y0 + statistics.median(s['size'] for s in spans) * 1.3)
            if i+1 < len(rows):
                next_top = min(r.y0 for r in rows[i+1]['boxes'])
                if next_top > rect.y1:
                    frame.y1 = min(frame.y1, next_top - .2)
            lines = []
            for line, span in cell:
                if lines and lines[-1]['_original'] is line:
                    lines[-1]['spans'].append(span)
                    lines[-1]['bbox'] = tuple(bounds(lines[-1]['spans']))
                else:
                    lines.append({**line, 'spans': [span], 'bbox': span['bbox'], '_original': line})
            added.append({'type': 0, 'bbox': tuple(rect), 'lines': lines, 'table_cell': True, 'cell_rect': list(frame)})
    kept = []
    for raw in data['blocks']:
        if raw.get('type') != 0:
            kept.append(raw)
            continue
        lines = []
        for line in raw['lines']:
            spans = [s for s in line['spans'] if id(s) not in taken]
            if spans:
                lines.append({**line, 'spans': spans, 'bbox': tuple(bounds(spans))})
        if lines:
            r = fitz.Rect(lines[0]['bbox'])
            for line in lines[1:]:
                r |= fitz.Rect(line['bbox'])
            kept.append({**raw, 'lines': lines, 'bbox': tuple(r)})
    data['blocks'] = kept + added
