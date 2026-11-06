"""Local, bounded paragraph flow around immutable page objects."""
from __future__ import annotations

from collections import Counter
import html
import io

import numpy as np
import pymupdf as fitz

from .models import checkpoint
from .structure import MATH_MARKER


def local_obstacles(block, obstacles):
    """Map unrotated PDF coordinates into the paragraph's reading direction."""
    r = fitz.Rect(block.rect)
    angle = round(block.angle / 90) * 90 % 360
    if abs((block.angle - angle + 180) % 360 - 180) > 1 or block.vertical:
        return None if obstacles else []
    width, height = (r.height, r.width) if angle in (90, 270) else (r.width, r.height)
    def point(x, y):
        x, y = x - r.x0, y - r.y0
        return {0: (x, y), 90: (r.height - y, x), 180: (r.width - x, r.height - y),
                270: (y, r.width - x)}[angle]
    result = []
    for obstacle in obstacles:
        o = fitz.Rect(obstacle) & r
        if o.is_empty:
            continue
        points = [point(p.x, p.y) for p in o.quad]
        result.append(fitz.Rect(min(p[0] for p in points), min(p[1] for p in points),
                                max(p[0] for p in points), max(p[1] for p in points)) & fitz.Rect(0, 0, width, height))
    return result


def flow_regions(bounds, obstacles, minimum_width=12):
    """Horizontal bands, using the widest safe side of each obstruction.

    Adjacent equal-width bands become a single frame so a line isn't split at
    arbitrary glyph edges. The paragraph remains one continuous Story.
    """
    obstacles = [r & bounds for r in obstacles if r.intersects(bounds)]
    if not obstacles:
        return [bounds]
    ys = sorted({bounds.y0, bounds.y1, *(v for r in obstacles for v in (r.y0, r.y1))})
    regions = []
    for y0, y1 in zip(ys, ys[1:]):
        if y1 - y0 < .01:
            continue
        intervals = [(bounds.x0, bounds.x1)]
        for o in obstacles:
            if o.y0 >= y1 or o.y1 <= y0:
                continue
            intervals = [(a, b) for left, right in intervals
                         for a, b in ((left, min(right, o.x0)), (max(left, o.x1), right)) if b - a >= minimum_width]
        if not intervals:
            continue
        left, right = max(intervals, key=lambda pair: pair[1] - pair[0])
        if regions and abs(regions[-1].y1 - y0) < .01 and abs(regions[-1].x0 - left) < .01 and abs(regions[-1].x1 - right) < .01:
            regions[-1].y1 = y1
        else:
            regions.append(fitz.Rect(left, y0, right, y1))
    return regions


def flow_paragraph(page, block, css, archive, minimum, font, source, obstacles=(), cancel=None):
    """Fit a paragraph without stretching formulas or overlapping obstacles.

    Dry placement uses at most eight size trials. Only the selected trial is
    drawn; no repeated PDF import or API request is needed.
    """
    if Counter(MATH_MARKER.findall(block.translation)) != Counter(i["marker"] for i in block.inline):
        block.note("段内公式占位符缺失或重复，保留原文，请恢复公式占位符后导出。")
        return None
    if block.inline and source is None:
        return None
    regions = flow_regions(page.rect, obstacles, max(8, minimum * 1.5))
    if not regions:
        return None
    archive = archive or fitz.Archive()
    if block.inline:
        blank = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 2), False)
        blank.clear_with(255)
        archive.add((blank.tobytes("png"), "inline-blank.png"))
    by_marker = {i["marker"]: i for i in block.inline}
    order = MATH_MARKER.findall(block.translation)
    value = html.escape(block.translation).replace("\n", "<br/>")
    color = f"#{block.color & 0xffffff:06x}"
    weight = "bold" if block.bold else "normal"
    descent = max((max(0, i["rect"][3] - i.get("baseline", i["rect"][3])) for i in block.inline), default=0)
    leading = max(font * 1.2, max((fitz.Rect(i["rect"]).height for i in block.inline), default=0) + descent + .5)

    def make_story(scale):
        def replace(match):
            item = by_marker[match.group()]
            r = fitz.Rect(item["rect"])
            # HTML width/height attributes are truncated to integer points by
            # MuPDF. CSS lengths retain the fractional geometry of glyphs.
            return (f'<img src="inline-blank.png" style="width:{r.width * scale:.5f}pt;'
                    f'height:{r.height * scale:.5f}pt"/>')
        text = MATH_MARKER.sub(replace, value)
        # Inline images contribute their full height to the line box. A larger
        # base leading leaves room for accents and CJK descenders.
        markup = f'<p style="font-size:{font * scale:.5f}pt;color:{color};font-weight:{weight};line-height:{leading * scale:.5f}pt">{text}</p>'
        return fitz.Story(html=markup, user_css="body{margin:0;}" + css, archive=archive)

    def fits(scale):
        checkpoint(cancel)
        story = make_story(scale)
        for region in regions:
            frame = region + (0, 0, 0, -descent * scale)
            if frame.is_empty:
                continue
            more, _ = story.place(frame)
            if not more:
                return True
        return False

    low, high = max(.05, minimum / font), 1.0
    if fits(high):
        scale = high
    elif not fits(low):
        return None
    else:
        for _ in range(6):
            mid = (low + high) / 2
            if fits(mid):
                low = mid
            else:
                high = mid
        scale = low
    story = make_story(scale)
    stream = io.BytesIO()
    writer = fitz.DocumentWriter(stream)
    device = writer.begin_page(page.rect)
    try:
        for region in regions:
            checkpoint(cancel)
            frame = region + (0, 0, 0, -descent * scale)
            if frame.is_empty:
                continue
            more, _ = story.place(frame)
            story.draw(device)
            if not more:
                break
    finally:
        writer.end_page()
        writer.close()
    with fitz.open(stream=stream.getvalue(), filetype="pdf") as flowed:
        images = flowed[0].get_image_info()
        if len(images) != len(order):
            block.note("段内公式排版未完整定位，保留原文。")
            return None
        if order:
            original = source[block.page]
            rotation = original.rotation
            original.set_rotation(0)
            try:
                for marker, image in zip(order, images):
                    target = fitz.Rect(image["bbox"])
                    item = by_marker[marker]
                    original_rect = fitz.Rect(item["rect"])
                    # Equal scales preserve symbols, hats and superscripts.
                    if abs(target.width / original_rect.width - target.height / original_rect.height) > .002:
                        block.note("公式纵横比例校验未通过，保留原文。")
                        return None
                    # Story ignores CSS vertical-align for inline images.
                    # Its image bottom is the text baseline: move the original
                    # formula down by its descent, reserving that space above.
                    dy = max(0, original_rect.y1 - item.get("baseline", original_rect.y1)) * scale
                    target += (0, dy, 0, dy)
                    if not (page.rect + (-.05, -.05, .05, .05)).contains(target):
                        block.note("公式上下标超出段落边界，保留原文。")
                        return None
                    probe = target + (.25, .25, -.25, -.25)
                    if not probe.is_empty:
                        pix = flowed[0].get_pixmap(matrix=fitz.Matrix(2, 2), clip=probe, alpha=False)
                        if np.any(np.frombuffer(pix.samples, dtype=np.uint8) < 200):
                            block.note("公式与段落文字相交，保留原文，请调整区域。")
                            return None
                    flowed[0].show_pdf_page(target, source, block.page, clip=original_rect, keep_proportion=True)
            finally:
                original.set_rotation(rotation)
        # Rendering the already-clipped result avoids false intersections from
        # invisible source resources imported with the formula PDF forms.
        for o in obstacles:
            clip = (fitz.Rect(o) + (.3, .3, -.3, -.3)) & page.rect
            if not clip.is_empty:
                pix = flowed[0].get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=True)
                if any(pix.samples[3::4]):
                    block.note("译文与图片或相邻内容相交，保留原文，请调整区域。")
                    return None
        page.show_pdf_page(page.rect, flowed, 0, keep_proportion=True)
    return scale


def vertical_paragraph(page, block, css, archive, minimum, font, source, cancel=None):
    """Stack glyph cells and indivisible horizontal vector formulas in columns."""
    if Counter(MATH_MARKER.findall(block.translation)) != Counter(i['marker'] for i in block.inline):
        block.note("段内公式占位符缺失或重复，保留原文，请恢复公式占位符后导出。")
        return None
    if block.inline and source is None:
        return None
    by_marker = {i['marker']: i for i in block.inline}
    tokens, end = [], 0
    for match in MATH_MARKER.finditer(block.translation):
        tokens.extend(c for c in block.translation[end:match.start()] if not c.isspace())
        tokens.append(match.group())
        end = match.end()
    tokens.extend(c for c in block.translation[end:] if not c.isspace())

    def pack(size):
        columns, column, y, width = [], [], 0, 0
        for token in tokens:
            if token in by_marker:
                r = fitz.Rect(by_marker[token]['rect'])
                w, h = r.width * size / font + 1, max(size * 1.6, r.height * size / font + 1)
            else:
                w, h = size * 1.5, size * 1.6
            if h > page.rect.height or w > page.rect.width:
                return None
            if column and y + h > page.rect.height:
                columns.append((column, width))
                column, y, width = [], 0, 0
            column.append((token, y, w, h))
            width, y = max(width, w), y + h
        if column:
            columns.append((column, width))
        return columns if sum(w for _, w in columns) + max(0, len(columns)-1) * size * .15 <= page.rect.width else None

    size = font
    while size >= minimum - .001:
        checkpoint(cancel)
        columns = pack(size)
        if columns is not None:
            break
        size -= .25
    else:
        block.note("竖排公式在原区域内无法完整放下，保留原文，请调整区域。")
        return None
    right = page.rect.width
    original = source[block.page] if block.inline else None
    rotation = original.rotation if original is not None else 0
    if original is not None:
        original.set_rotation(0)
    try:
        for column, width in columns:
            left = right - width
            for token, y, w, h in column:
                checkpoint(cancel)
                if token in by_marker:
                    r = fitz.Rect(by_marker[token]['rect'])
                    rw, rh = r.width * size / font, r.height * size / font
                    target = fitz.Rect(left + (width-rw)/2, y + (h-rh)/2, left + (width+rw)/2, y + (h+rh)/2)
                    page.show_pdf_page(target, source, block.page, clip=r, keep_proportion=True)
                else:
                    box = fitz.Rect(left, y, right, y+h)
                    markup = f'<p style="font-size:{size:.5f}pt;color:#{block.color & 0xffffff:06x};font-weight:{"bold" if block.bold else "normal"}">{html.escape(token)}</p>'
                    spare, _ = page.insert_htmlbox(box, markup, css=css, archive=archive, scale_low=1)
                    if spare < 0:
                        return None
            right = left - size * .15
    finally:
        if original is not None:
            original.set_rotation(rotation)
    return size / font
