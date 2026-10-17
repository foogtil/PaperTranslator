"""Document translation units: paragraphs and complete figures, independent of PDF spans."""
from __future__ import annotations

from collections import Counter
import re
import statistics

import pymupdf as fitz

from .models import Block, Unit

CAPTION = re.compile(r"^\s*(?:fig(?:ure)?\.?\s*\d+[a-z]?|图\s*[一二三四五六七八九十\d]+)\b", re.I)
MATH_MARKER = re.compile(r"\[\[MATH_\d+\]\]")


def inside(rect, container, threshold=0.65):
    rect, container = fitz.Rect(rect), fitz.Rect(container)
    return (rect & container).get_area() / max(0.01, rect.get_area()) >= threshold


def figure_rects(page):
    """Combine related raster / vector panels, using captions when available.

    Full-page scanned images are handled as document pages, not single figures.
    Nearby graphics are merged only if there is no body paragraph between them.
    """
    page_rect = page.rect
    text = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)
    lines = [(fitz.Rect(line["bbox"]), "".join(s["text"] for s in line["spans"]))
             for b in text["blocks"] if b.get("type") == 0 for line in b["lines"]]
    captions = [(r, t) for r, t in lines if CAPTION.match(t)]
    candidates = []
    for image in page.get_image_info():
        rect = fitz.Rect(image["bbox"]) & page_rect
        if min(rect.width, rect.height) >= 32 and 0.008 <= rect.get_area() / page_rect.get_area() <= 0.70:
            candidates.append(rect)
    drawings = page.get_drawings()
    if drawings:
        for rect in page.cluster_drawings(drawings=drawings, x_tolerance=12, y_tolerance=12):
            rect &= page_rect
            if min(rect.width, rect.height) < 30 or rect.get_area() < page_rect.get_area() * 0.008:
                continue
            contained_prose = sum(len(t) > 80 and inside(r, rect) for r, t in lines)
            has_caption = any(-20 <= c.y0 - rect.y1 <= 120 and (min(c.x1, rect.x1) - max(c.x0, rect.x0)) > 20 for c, _ in captions)
            if not has_caption and min(rect.width, rect.height) < 45:
                continue
            # Large tables / page borders containing body prose are not figures.
            if contained_prose < 4 or has_caption:
                candidates.append(rect)
    # Contained graphics belong to the same image / chart.
    merged = []
    for rect in sorted(candidates, key=lambda r: -r.get_area()):
        if not any(inside(rect, old, 0.90) for old in merged):
            merged.append(rect)
    # A shared caption joins multi-panel graphics across the width of the figure.
    for caption, _ in captions:
        below = [r for r in merged if -8 <= caption.y0 - r.y1 <= 140 and r.x1 > caption.x0 - 30]
        if not below:
            continue
        bottom = max(r.y1 for r in below)
        panels = [r for r in merged if r.y1 <= bottom + 20 and r.y1 >= bottom - 180 and r.y0 < caption.y0
                  and not any(len(t) > 90 and r.y1 + 4 < lr.y0 < caption.y0 - 4 and lr.x0 < r.x1 and lr.x1 > r.x0 for lr, t in lines)]
        if len(panels) > 1:
            joined = fitz.Rect(panels[0])
            for panel in panels[1:]:
                joined |= panel
            merged = [r for r in merged if r not in panels] + [joined]
    return sorted(merged, key=lambda r: (r.y0, r.x0))


def logical_rows(raw, hard_protected, figures=()):
    """Reconstruct visual lines, including superscripts split into PDF lines."""
    runs = [(line, span, "".join(c["c"] for c in span.get("chars", []))) for line in raw.get("lines", [])
            for span in line["spans"] if any(not c["c"].isspace() for c in span.get("chars", []))]
    if not runs:
        return []
    if any(abs(line["dir"][0] - 1) > 0.01 or line.get("wmode") for line, _, _ in runs):
        return None  # Nonhorizontal labels keep their original geometry.
    sizes = Counter()
    for _, span, value in runs:
        sizes[round(span["size"], 1)] += len(value.strip())
    typical = sizes.most_common(1)[0][0]
    prose_baselines = [r for r in runs if re.search(r"[A-Za-z]{3,}|[\u3400-\u9fff]{2,}", r[2])
                       and not hard_protected(r[1], r[2], typical)]
    prose_block = (len(re.findall(r"[A-Za-z]{3,}|[\u3400-\u9fff]{2,}", " ".join(r[2] for r in runs))) >= 12
                   and fitz.Rect(raw["bbox"]).width < 350 and not any(inside(raw["bbox"], f) for f in figures))
    baseline_runs = (prose_baselines or runs) if prose_block else runs
    baselines = []
    for _, span, _ in sorted(baseline_runs, key=lambda r: r[1]["origin"][1]):
        y = span["origin"][1]
        if not baselines or y - baselines[-1] > min(typical, span["size"]) * 0.65:
            baselines.append(y)
    if not baselines:
        return None
    grouped = [[] for _ in baselines]
    for line, span, value in runs:
        i = min(range(len(baselines)), key=lambda n: abs(baselines[n] - span["origin"][1]))
        grouped[i].append((line, span, value, False))
    rows = []
    for parts in grouped:
        row_sizes = Counter()
        for _, span, value, _ in parts:
            row_sizes[round(span["size"], 1)] += len(value.strip())
        row_typical = row_sizes.most_common(1)[0][0]
        parts = [(line, span, value, hard_protected(span, value, row_typical) or span["size"] < row_typical * 0.85
                  or any(ord(c) < 32 and not c.isspace() for c in value)) for line, span, value, _ in parts]
        parts.sort(key=lambda r: (r[1]["bbox"][0], r[1]["origin"][1]))
        current = []
        for part in parts:
            # Distant columns and table cells remain distinct.
            gap = part[1]["bbox"][0] - current[-1][1]["bbox"][2] if current else 0
            line_widths = (part[0]["bbox"][2] - part[0]["bbox"][0], current[-1][0]["bbox"][2] - current[-1][0]["bbox"][0]) if current else (0, 0)
            separate_lines = (current and part[0] is not current[-1][0] and max(line_widths) > 120 and min(line_widths) > 20
                              and part[0]["bbox"][0] - current[-1][0]["bbox"][2] > typical * 0.6)
            if current and ((gap > typical * 3 and not prose_block) or separate_lines):
                rows.append(current)
                current = []
            current.append(part)
        if current:
            rows.append(current)
    return rows


def paragraph_runs(raw, hard_protected, run_text, figures):
    rows = logical_rows(raw, hard_protected, figures)
    if rows is None:
        return None
    if not rows:
        return []
    # Some PDFs store both caption columns in one text block. Restore column
    # reading order before joining physical lines into paragraphs.
    boxes = [fitz.Rect(min(r[1]["bbox"][0] for r in row), min(r[1]["bbox"][1] for r in row),
                       max(r[1]["bbox"][2] for r in row), max(r[1]["bbox"][3] for r in row)) for row in rows]
    for a, b in zip(boxes, boxes[1:]):
        if abs(a.y0 - b.y0) < 4 and min(a.width, b.width) > 20 and max(a.width, b.width) > 100 and b.x0 > a.x1:
            cut = (a.x1 + b.x0) / 2
            if all(r.x1 <= cut + 2 or r.x0 >= cut - 2 for r in boxes):
                rows = [row for _, row in sorted(zip(boxes, rows), key=lambda item: (item[0].x0 >= cut, item[0].y0))]
                break
    margin = min(r[0][1]["bbox"][0] for r in rows)
    font_weights = Counter()
    for row in rows:
        for _, span, value, _ in row:
            font_weights[span.get("font", "")] += len(value)
    common_font = font_weights.most_common(1)[0][0]
    prose_block = len(re.findall(r"[A-Za-z]{3,}|[\u3400-\u9fff]{2,}", " ".join(run_text(row) for row in rows))) >= 12 and fitz.Rect(raw["bbox"]).width < 350
    result, current = [], []
    previous = None
    for row in rows:
        rect = fitz.Rect(row[0][1]["bbox"])
        for run in row[1:]:
            rect |= fitz.Rect(run[1]["bbox"])
        prose_sizes = [r[1]["size"] for r in row if not r[3] and re.search(r"[A-Za-z]{3,}|[\u3400-\u9fff]{2,}", r[2])]
        size = statistics.median(prose_sizes or [r[1]["size"] for r in row])
        words = re.findall(r"[A-Za-z]{3,}|[\u3400-\u9fff]{2,}", run_text(row))
        in_figure = any(inside(rect, f) for f in figures)
        heading = (any(r[1].get("flags", 0) & 16 or r[1].get("font", "").lower().endswith(".b") for r in row)
                   or (len(rows) > 2 and all(r[1].get("font", "") != common_font and not r[3] for r in row))) and len(run_text(row)) < 110
        standalone = in_figure or not words or all(r[3] for r in row)
        if previous:
            old_rect, old_size, old_heading, old_standalone, old_text = previous
            indent = (rect.x0 > margin + size * 0.8 and rect.x0 - old_rect.x0 > size * 0.7
                      and bool(re.search(r"[.!?:。！？：]$", old_text.strip())))
            split = (standalone or old_standalone or heading != old_heading or rect.y0 - old_rect.y0 > max(size, old_size) * 2.1
                     or rect.y0 < old_rect.y0 - size * 0.3 or abs(size - old_size) > max(size, old_size) * 0.16
                     or (abs(rect.y0 - old_rect.y0) < size * 0.5 and rect.x0 - old_rect.x1 > size * 2)
                     or (not prose_block and min(rect.x1, old_rect.x1) - max(rect.x0, old_rect.x0) < min(rect.width, old_rect.width) * 0.15 and abs(rect.x0 - old_rect.x0) > size * 3)
                     or indent or (old_rect.x1 < rect.x1 - size * 3 and re.search(r"[.!?:。！？：]$", old_text.strip())))
            if split and current:
                result.append(current)
                current = []
        current.extend(row)
        previous = rect, size, heading, standalone, run_text(row)
    if current:
        result.append(current)
    return result


def paragraph_template(runs, run_text):
    """Math is an immutable inline object, never an independent translation."""
    inline, modified = [], []
    group = []
    def flush():
        if not group:
            return
        rect = fitz.Rect(group[0][1]["bbox"])
        for run in group[1:]:
            rect |= fitz.Rect(run[1]["bbox"])
        marker = f"[[MATH_{len(inline)}]]"
        inline.append({"marker": marker, "rect": list(rect), "text": run_text(group)})
        for i, (line, span, _, hard) in enumerate(group):
            modified.append((line, span, marker if i == 0 else "", hard))
        group.clear()
    for run in runs:
        if run[3]:
            overlap = min(run[1]["bbox"][2], group[-1][1]["bbox"][2]) - max(run[1]["bbox"][0], group[-1][1]["bbox"][0]) if group else 0
            if group and overlap <= 0 and abs(run[1]["origin"][1] - group[-1][1]["origin"][1]) > max(run[1]["size"], group[-1][1]["size"]) * 0.9:
                flush()
            group.append(run)
        else:
            flush()
            modified.append(run)
    flush()
    return run_text(modified), inline


def assign_units(project, figures_by_page=None):
    """Use one parent unit per paragraph / figure; children retain label coordinates."""
    figures_by_page = figures_by_page or {p: [u.rect for u in project.units if u.page == p and u.kind == "figure"] for p in project.selected_pages}
    units = []
    for page in project.selected_pages:
        blocks = [b for b in project.blocks if b.page == page]
        for b in blocks:
            b.unit_id = ""
        figures = []
        for n, rect in enumerate(figures_by_page.get(page, [])):
            members = [b for b in blocks if inside(b.rect, rect)]
            unit = Unit(f"p{page + 1}-figure{n + 1}", page, "figure", list(rect), f"图片 {n + 1}")
            for b in members:
                b.unit_id, b.role = unit.id, "figure_label"
                unit.block_ids.append(b.id)
            figures.append(unit)
        # Attach the complete caption, including a continuation in a second column.
        captions = [b for b in blocks if not b.unit_id and CAPTION.match(b.text)]
        for caption in captions:
            r = fitz.Rect(caption.rect)
            candidates = [u for u in figures if -10 <= r.y0 - u.rect[3] <= 150]
            if not candidates:
                continue
            unit = min(candidates, key=lambda u: abs(r.y0 - u.rect[3]))
            unit.title = re.split(r"[|：:]", caption.text, maxsplit=1)[0][:60]
            caption.unit_id, caption.role = unit.id, "figure_caption"
            unit.block_ids.append(caption.id)
            for b in blocks:
                br = fitz.Rect(b.rect)
                if not b.unit_id and abs(br.y0 - r.y0) < max(6, caption.fontsize) and br.x0 >= r.x1 - 3 and abs(br.y1 - r.y1) < max(20, r.height * 0.35):
                    b.unit_id, b.role = unit.id, "figure_caption"
                    unit.block_ids.append(b.id)
        for unit in figures:
            # Include the caption in the preview selection without changing the image area used by OCR.
            unit.block_ids.sort(key=lambda identity: next((b.rect[1], b.rect[0]) for b in blocks if b.id == identity))
            units.append(unit)
        for b in blocks:
            if not b.unit_id:
                b.unit_id = b.id
                if b.role == "text":
                    b.role = "paragraph" if not b.protected else "protected"
                units.append(Unit(b.id, page, b.role, b.rect.copy(), "段落" if not b.protected else "保留内容", [b.id]))
    project.units = sorted(units, key=lambda u: (u.page, u.rect[1], u.rect[0]))


def unit_counts(project):
    return Counter(u.kind for u in project.units)


def join_continuations(blocks, figures):
    """Reconnect a paragraph fragmented across PDF objects by inline mathematics."""
    result = []
    for block in blocks:
        if not result:
            result.append(block)
            continue
        previous = result[-1]
        a, b = fitz.Rect(previous.rect), fitz.Rect(block.rect)
        size = max(previous.fontsize, block.fontsize)
        start = re.search(r"[A-Za-z\u3400-\u9fff]", block.text)
        can_join = (not previous.protected and not block.protected and previous.kind == block.kind == "native"
                    and previous.angle == block.angle == 0 and not previous.bold and not block.bold
                    and min(a.width, b.width) > size * 10 and abs(a.x0 - b.x0) < size * 2
                    and -size * 2 < b.y0 - a.y1 < size * 1.8 and b.y1 > a.y1
                    and abs(previous.fontsize - block.fontsize) < size * 0.15
                    and not re.search(r"[.!?:;。！？：；][\"'’”)]*$", previous.text.rstrip())
                    and start and start.group().islower()
                    and not any(inside(a, f) or inside(b, f) for f in figures))
        if not can_join:
            result.append(block)
            continue
        template = block.template or block.text
        offset = len(previous.inline)
        replacements = {item["marker"]: f"[[MATH_{offset + n}]]" for n, item in enumerate(block.inline)}
        template = MATH_MARKER.sub(lambda m: replacements.get(m.group(), m.group()), template)
        previous.template = (previous.template or previous.text) + " " + template
        previous.inline.extend({**item, "marker": replacements[item["marker"]]} for item in block.inline)
        previous.text = previous.text.rstrip() + " " + block.text.lstrip()
        previous.erasers.extend(block.erasers)
        rect = a | b
        previous.rect = list(rect)
        previous.quad = [list(rect.tl), list(rect.tr), list(rect.br), list(rect.bl)]
        previous.local_width, previous.local_height = rect.width, rect.height
        previous.confidence = min(previous.confidence, block.confidence)
    return result


def merge_ocr_paragraphs(blocks, figures):
    """Group adjacent OCR body lines, while keeping figure label coordinates."""
    eligible = [b for b in blocks if b.kind == "raster" and not b.protected and b.angle % 360 < 1
                and not b.vertical and not any(inside(b.rect, f) for f in figures)]
    eligible.sort(key=lambda b: (b.rect[1], b.rect[0]))
    groups = []
    for b in eligible:
        candidates = []
        for group in groups:
            old = group[-1]
            size = max(old.fontsize, b.fontsize)
            overlap = min(old.rect[2], b.rect[2]) - max(old.rect[0], b.rect[0])
            if (0 < b.rect[1] - old.rect[1] < size * 2.2 and abs(old.fontsize - b.fontsize) < size * 0.2
                and overlap > min(old.rect[2] - old.rect[0], b.rect[2] - b.rect[0]) * 0.65
                and not (b.rect[0] - old.rect[0] > size * 0.7 and re.search(r"[.!?。！？]$", old.text))):
                candidates.append(group)
        if candidates:
            min(candidates, key=lambda g: abs(b.rect[1] - g[-1].rect[1])).append(b)
        else:
            groups.append([b])
    removed = set()
    for group in groups:
        first = group[0]
        if len(group) < 2:
            first.role = "paragraph"
            continue
        rect = fitz.Rect(first.rect)
        for b in group[1:]:
            rect |= fitz.Rect(b.rect)
            for note in b.notes:
                first.note(note)
            removed.add(b.id)
        first.text = re.sub(r"(?<=\w)-\n(?=[a-z])", "", "\n".join(b.text for b in group)).replace("\n", " ")
        first.rect = list(rect)
        first.quad = [list(rect.tl), list(rect.tr), list(rect.br), list(rect.bl)]
        first.local_width, first.local_height = rect.width, rect.height
        first.confidence = min(b.confidence for b in group)
        first.role = "paragraph"
    return [b for b in blocks if b.id not in removed]
