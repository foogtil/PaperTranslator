from __future__ import annotations

from collections import Counter
import html
import math
import os
from pathlib import Path
import tempfile

import cv2
import numpy as np
import pymupdf as fitz

from . import __version__
from .models import Block, Options, Project, atomic_json, checkpoint, digest_file


def _inline_page(page, block, markup, css, archive, minimum, font, source):
    """Flow one translated paragraph around immutable formula objects.

    Story lays out temporary blank inline images. Their actual PDF image boxes
    locate the original vector formula snippets, which replace the blanks.
    """
    from .structure import MATH_MARKER
    if Counter(MATH_MARKER.findall(block.translation)) != Counter(i["marker"] for i in block.inline):
        block.note("段内公式占位符缺失或重复，保留原文，请恢复公式占位符后导出。")
        return None
    archive = archive or fitz.Archive()
    blank = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 2), False)
    blank.clear_with(255)
    archive.add((blank.tobytes("png"), "inline-blank.png"))
    by_marker = {i["marker"]: i for i in block.inline}
    order = MATH_MARKER.findall(block.translation)
    def replace(match):
        rect = fitz.Rect(by_marker[match.group(0)]["rect"])
        return f'<img src="inline-blank.png" width="{rect.width:.3f}" height="{rect.height:.3f}"/>'
    markup = MATH_MARKER.sub(replace, markup)
    story = fitz.Story(html=markup, user_css="body{margin:0;}" + css, archive=archive)
    fit = story.fit_scale(page.rect, scale_min=1, scale_max=1 / max(0.05, minimum / font))
    if not fit.big_enough:
        return None
    with story.write_with_links(lambda *args: (fit.rect, fit.rect, None)) as flowed:
        images = flowed[0].get_image_info()
        if len(images) != len(order):
            block.note("段内公式排版未完整定位，保留原文。")
            return None
        original = source[block.page]
        rotation = original.rotation
        original.set_rotation(0)
        try:
            for marker, image in zip(order, images):
                flowed[0].show_pdf_page(fitz.Rect(image["bbox"]), source, block.page,
                                        clip=fitz.Rect(by_marker[marker]["rect"]), keep_proportion=False)
        finally:
            original.set_rotation(rotation)
        page.show_pdf_page(page.rect, flowed, 0, keep_proportion=False)
    return 1 / fit.parameter


def _text_page(scratch, block: Block, options: Options, source=None):
    angle = block.angle % 360
    rect = fitz.Rect(block.rect)
    cardinal = round(angle / 90) * 90 % 360
    if min(abs(angle - cardinal), 360 - abs(angle - cardinal)) < 1:
        width, height = (rect.height, rect.width) if cardinal in (90, 270) else (rect.width, rect.height)
        angle = cardinal
    else:
        width = block.local_width or rect.width
        height = block.local_height or rect.height
    if width < 0.5 or height < 0.5:
        return None
    page = scratch.new_page(width=width, height=height)
    font = max(3.0, block.fontsize)
    minimum = min(font, options.minimum_font)
    css = "* {margin:0;padding:0;} body {font-family:sans-serif;} p {margin:0;line-height:1.08;}"
    archive = None
    if options.font_path:
        font_file = Path(options.font_path).resolve()
        archive = fitz.Archive(str(font_file.parent))
        css += f"@font-face {{font-family:custom;src:url('{font_file.name}');}} body {{font-family:custom;}}"
    if block.vertical:
        characters = [c for c in block.translation if not c.isspace()]
        size = font
        while size >= minimum - 0.001:
            per_column = max(1, int(height / (size * 1.60)))
            needed = math.ceil(len(characters) / per_column)
            available = max(0, int(width / (size * 1.5)))
            if needed <= available:
                break
            size -= 0.25
        else:
            return None
        for column in range(needed):
            x1 = width - column * size * 1.5
            box = fitz.Rect(max(0, x1 - size * 1.5), 0, x1, height)
            text = "<br/>".join(html.escape(c) for c in characters[column * per_column:(column + 1) * per_column])
            spare, _ = page.insert_htmlbox(box, f'<p style="font-size:{size:.3f}pt;line-height:1.08">{text}</p>', css=css, archive=archive, scale_low=1)
            if spare < 0:
                return None
        return page.number, angle, size / font
    value = html.escape(block.translation)
    value = value.replace("\n", "<br/>")
    color = f"#{block.color & 0xFFFFFF:06x}"
    weight = "bold" if block.bold else "normal"
    markup = f'<p style="font-size:{font:.3f}pt;color:{color};font-weight:{weight}">{value}</p>'
    if block.inline:
        if source is None:
            return None
        scale = _inline_page(page, block, markup, css, archive, minimum, font, source)
        return None if scale is None else (page.number, angle, scale)
    spare, scale = page.insert_htmlbox(page.rect, markup, css=css, archive=archive, scale_low=max(0.05, minimum / font))
    if spare < 0:
        return None
    return page.number, angle, scale


def _raster_patch(page, block: Block, approved: bool):
    if block.confidence < 0.75 and not approved:
        return None, "OCR 置信度低于 75%，保留原图，需核对并确认。"
    dpi = 300
    zoom = dpi / 72
    clip = (fitz.Rect(block.rect) + (-1, -1, 1, 1)) & page.rect
    if clip.is_empty:
        return None, "文字区域超出页面。"
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].copy()
    # pix.x / pix.y include rounding of the clipped raster's global origin.
    polygon = np.array([[(x * zoom - pix.x), (y * zoom - pix.y)] for x, y in (block.quad or [[clip.x0, clip.y0], [clip.x1, clip.y0], [clip.x1, clip.y1], [clip.x0, clip.y1]])], dtype=np.int32)
    allowed = np.zeros(rgb.shape[:2], dtype=np.uint8)
    cv2.fillPoly(allowed, [polygon], 255)
    border = np.concatenate((rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]), axis=0)
    background = np.median(border, axis=0)
    # A spatially complex background cannot be repaired reliably by a flat fill.
    boundary_variation = np.percentile(np.linalg.norm(border.astype(float) - background, axis=1), 70)
    if boundary_variation > 65 and not approved:
        return None, "图中文字背景复杂，需人工确认后替换，当前保留原图。"
    distance = np.linalg.norm(rgb.astype(float) - background, axis=2)
    mask = ((distance > 10) & (allowed > 0)).astype(np.uint8) * 255
    # Preserve long chart axes and grid lines even when crossing the text box.
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((1, max(12, int(pix.width * 0.72))), np.uint8))
    vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((max(12, int(pix.height * 0.88)), 1), np.uint8))
    for row in range(horizontal.shape[0]):
        if not (np.any(horizontal[row, :3]) and np.any(horizontal[row, -3:])):
            horizontal[row] = 0
    for column in range(vertical.shape[1]):
        if not (np.any(vertical[:3, column]) and np.any(vertical[-3:, column])):
            vertical[:, column] = 0
    protected = cv2.bitwise_or(horizontal, vertical)
    if np.any(protected):
        block.note("图中文字区域含轴线或网格，请对照原图检查替换结果。")
    mask = cv2.bitwise_and(mask, cv2.bitwise_not(protected))
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)
    mask = cv2.bitwise_and(mask, allowed)
    if np.count_nonzero(mask) / max(1, np.count_nonzero(allowed)) > 0.65 and not approved:
        return None, "图中文字与图形重叠较多，需人工确认，当前保留原图。"
    repaired = cv2.inpaint(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), mask, 3, cv2.INPAINT_TELEA)
    if boundary_variation < 12:
        repaired[mask > 0] = background[::-1].astype(np.uint8)
    # Replace only erased glyph pixels; the transparent surroundings retain
    # vector lines and avoid reintroducing text from neighbouring patches.
    repaired_alpha = np.dstack((repaired, mask))
    ok, encoded = cv2.imencode(".png", repaired_alpha)
    if not ok:
        return None, "图像修补编码失败。"
    # Use the exact pixel extent as the insertion rectangle to prevent subpixel
    # stretching when PDF clip coordinates were rounded by the rasterizer.
    actual_rect = fitz.Rect(pix.x / zoom, pix.y / zoom, (pix.x + pix.width) / zoom, (pix.y + pix.height) / zoom)
    return (actual_rect, encoded.tobytes()), ""


def _restore_links(page, links):
    for link in page.get_links():
        page.delete_link(link)
    for link in links:
        value = {k: v for k, v in link.items() if k not in ("xref", "id")}
        if value.get("kind") in (fitz.LINK_GOTO, fitz.LINK_URI, fitz.LINK_GOTOR, fitz.LINK_LAUNCH, fitz.LINK_NAMED):
            try:
                page.insert_link(value)
            except (ValueError, RuntimeError):
                pass


def _add_redaction_quads(page, rectangles, cancel=None, on_batch=None):
    """Keep exact disjoint glyph boxes, with a bounded number of annotations.

    Creating one annotation per glyph repeatedly scans all annotation names.
    A PDF Redact annotation can instead carry multiple QuadPoints. Keep their
    coordinates separate so gaps, adjacent formulas and other text survive.
    """
    transform = ~page.transformation_matrix
    for start in range(0, len(rectangles), 256):
        checkpoint(cancel)
        batch = [fitz.Rect(value) for value in rectangles[start:start + 256]]
        bounds = fitz.Rect(batch[0])
        coordinates = []
        for rect in batch:
            bounds |= rect
            for point in rect.quad:
                point = point * transform
                coordinates.extend((point.x, point.y))
        annot = page.add_redact_annot(bounds, fill=False, cross_out=False)
        page.parent.xref_set_key(annot.xref, "QuadPoints",
                                 "[" + " ".join(f"{value:.9g}" for value in coordinates) + "]")
        if on_batch:
            on_batch(min(start + 256, len(rectangles)), len(rectangles))


def export_pdf(project: Project, output: str | Path, options: Options, cancel=None, log=None, progress=None):
    log = log or (lambda value: None)
    progress = progress or (lambda value: None)

    def step(phase, label, current=0, total=0, page=None, region=None, temporary=None):
        progress({"category": "export", "phase": phase, "label": label, "current": current, "total": total,
                  "page": None if page is None else page + 1, "region": region, "temporary": temporary})

    step("prepare", "检查原文与已保存译文")
    output = Path(output).resolve()
    if output == Path(project.source).resolve():
        raise ValueError("输出路径不能覆盖原 PDF。")
    if digest_file(project.source) != project.source_hash:
        raise ValueError("原 PDF 内容已变化，请重新分析。")
    output.parent.mkdir(parents=True, exist_ok=True)
    unresolved = []
    modifications = []
    with fitz.open(project.source) as document, fitz.open() as scratch:
        if document.needs_pass:
            raise ValueError("请先解锁加密的原 PDF。")
        # PDF graft maps snapshot the source object table. Build all text pages
        # before the first import so later additions cannot invalidate that map.
        text_prepared = {}
        candidates = [b for b in project.blocks if b.page in project.selected_pages and not b.protected
                      and b.translation.strip() and b.translation != b.text and b.status != "preserved"]
        log(f"开始预排版 {len(candidates)} 个译文区域。")
        for number, block in enumerate(candidates):
            checkpoint(cancel)
            step("typeset", "预排版译文", number, len(candidates), block.page, block.id)
            text_prepared[block.id] = _text_page(scratch, block, options, document)
        step("typeset", "预排版译文", len(candidates), len(candidates))
        for page_number, index in enumerate(project.selected_pages):
            checkpoint(cancel)
            page = document[index]
            rotation = page.rotation
            page.set_rotation(0)
            if not options.pdf_review_marks:
                for annot in list(page.annots() or []):
                    info = annot.info
                    if info.get("title") == "PaperTranslator 校对提示" and info.get("content", "").startswith("[待校对]"):
                        page.delete_annot(annot)
            links = page.get_links()
            existing_redactions = any(a.type[0] == fitz.PDF_ANNOT_REDACT for a in page.annots() or [])
            blocks = [b for b in project.blocks if b.page == index]
            prepared = []
            protected_rects = [fitz.Rect(b.rect) for b in blocks if b.protected]
            page_bounds = page.rect
            for number, block in enumerate(blocks):
                checkpoint(cancel)
                step("check_regions", "检查区域与图像背景", number, len(blocks), index, block.id)
                if block.notes and not block.approved:
                    unresolved.append(block.id)
                if block.protected or block.status == "preserved" or block.translation == block.text:
                    continue
                if not block.translation.strip():
                    block.note("缺少译文，输出中保留原文。")
                    unresolved.append(block.id)
                    continue
                rect = fitz.Rect(block.rect)
                if existing_redactions and (block.kind == "native" or block.erasers):
                    block.note("原页含尚未应用的涂黑批注；为保留原有批注，该区域保持原文。")
                    unresolved.append(block.id)
                    continue
                if rect.is_empty or not page_bounds.contains(rect):
                    block.note("文字区域超出页面，输出中保留原文。")
                    unresolved.append(block.id)
                    continue
                # Exact glyph-center erasers avoid deleting neighbouring math.
                if block.kind == "native" and any(any(p.intersects(fitz.Rect(e)) for p in protected_rects) for e in block.erasers):
                    block.note("文字与受保护公式重叠，保留原文，需人工调整区域。")
                    unresolved.append(block.id)
                    continue
                fit = text_prepared.get(block.id)
                if fit is None:
                    block.status = "overflow"
                    block.note(f"译文在原区域内无法以最低 {min(options.minimum_font, block.fontsize):g} pt 完整放下；保留原文。")
                    unresolved.append(block.id)
                    continue
                patch = None
                if block.kind == "raster":
                    step("repair_image", "修补图内文字", page=index, region=block.id)
                    patch, reason = _raster_patch(page, block, block.approved)
                    if patch is None:
                        block.note(reason)
                        unresolved.append(block.id)
                        continue
                prepared.append((block, fit, patch))
            # Prepare all image patches from the untouched source page first.
            erasers = [eraser for block, _, _ in prepared if block.erasers or block.kind == "native"
                       for eraser in block.erasers or [block.rect]]
            log(f"第 {index + 1} 页：准备 {len(erasers)} 个字形删除区域，批量处理。")
            step("prepare_erasers", "准备原文字形删除", 0, len(erasers), index)
            _add_redaction_quads(page, erasers, cancel,
                                 lambda done, total: step("prepare_erasers", "准备原文字形删除", done, total, index))
            if any(b.erasers or b.kind == "native" for b, _, _ in prepared):
                step("erase_text", "删除原文字形", page=index)
                page.apply_redactions(images=0, graphics=0, text=0)
            math_rects = [fitz.Rect(item["rect"]) for b, _, _ in prepared for item in b.inline]
            if math_rects:
                step("move_formulas", "调整段内公式线条", page=index)
                # Move fraction bars and other line art completely contained in
                # inline formula objects, without touching neighbouring text or
                # chart paths that merely cross a formula's box.
                for rect in math_rects:
                    page.add_redact_annot(rect, fill=False, cross_out=False)
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                      graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                      text=fitz.PDF_REDACT_TEXT_NONE)
            if prepared:
                _restore_links(page, links)
            for number, (block, fit, patch) in enumerate(prepared):
                checkpoint(cancel)
                step("write_text", "写回译文与公式", number, len(prepared), index, block.id)
                if patch:
                    page.insert_image(patch[0], stream=patch[1], overlay=True)
                scratch_index, angle, scale = fit
                page.show_pdf_page(fitz.Rect(block.rect), scratch, scratch_index, rotate=angle, keep_proportion=False, overlay=True)
                if block.status == "overflow":
                    block.status = "translated"
                modifications.append({"id": block.id, "page": index + 1, "rect": block.rect, "font_scale": scale})
                if block.notes and not block.approved:
                    unresolved.append(block.id)
            for block in blocks:
                if options.pdf_review_marks and block.id in unresolved:
                    point = fitz.Point(max(1, block.rect[0]), max(1, block.rect[1]))
                    if page.rect.contains(point):
                        note = page.add_text_annot(point, "[待校对] " + block.id + "\n" + "\n".join(block.notes) + "\n译文：" + block.translation)
                        note.set_info(title="PaperTranslator 校对提示")
            page.set_rotation(rotation)
            log(f"已排版第 {index + 1} 页。")
            step("page_done", "页面排版完成", page_number + 1, len(project.selected_pages), index)
        checkpoint(cancel)
        metadata = document.metadata
        metadata["producer"] = f"PaperTranslator {__version__} / PyMuPDF"
        document.set_metadata(metadata)
        # Subsetting embeds only used glyphs, keeping Chinese and other scripts
        # searchable on computers without installed source fonts.
        log("正在精简并嵌入字体。")
        step("fonts", "精简并嵌入字体")
        document.subset_fonts()
        checkpoint(cancel)
        fd, temporary = tempfile.mkstemp(prefix=".papertranslator-", suffix=".pdf", dir=output.parent)
        os.close(fd)
        try:
            log("正在压缩并写入 PDF。")
            step("save", "压缩并写入 PDF", temporary=temporary)
            document.save(temporary, garbage=4, deflate=True)
            checkpoint(cancel)
            step("verify", "校验输出 PDF", 0, len(project.pages), temporary=temporary)
            with fitz.open(temporary) as verify:
                if len(verify) != len(project.pages):
                    raise RuntimeError("导出校验失败：页数变化。")
                for number, (p, original) in enumerate(zip(verify, project.pages), 1):
                    checkpoint(cancel)
                    if p.rotation != original.rotation:
                        raise RuntimeError("导出校验失败：页面旋转变化。")
                    step("verify", "校验输出 PDF", number, len(project.pages), temporary=temporary)
            os.replace(temporary, output)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    step("report", "整理校对结果")
    report = {"source": project.source, "source_sha256": project.source_hash, "output": str(output),
              "pages": len(project.pages), "translated_regions": len(modifications), "needs_review": sorted(set(unresolved)),
              "usage": project.usage, "provider": project.provider, "options": vars(options).copy(),
              "modifications": modifications, "warnings": project.warnings,
              "units": [vars(u) for u in project.units],
              "regions": [{"id": b.id, "unit_id": b.unit_id, "role": b.role, "page": b.page + 1, "source": b.text, "translation": b.translation,
                           "status": b.status, "notes": b.notes, "approved": b.approved} for b in project.blocks]}
    project.output = str(output)
    project.options.update({"pdf_review_marks": options.pdf_review_marks, "export_json": options.export_json})
    if options.export_json:
        step("report", "保存 JSON 报告与校对项目")
        atomic_json(output.with_suffix(".report.json"), report)
        project.save(output.with_suffix(".ptrans.json"))
    step("complete", "PDF 导出完成", len(project.selected_pages), len(project.selected_pages))
    return report
