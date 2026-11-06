from __future__ import annotations

import html
import os
from pathlib import Path
import tempfile

import cv2
import numpy as np
import pymupdf as fitz

from . import __version__
from .models import Block, Options, Project, atomic_json, checkpoint, digest_file, parse_pages, block_enabled
from .flow import flow_paragraph, local_obstacles, vertical_paragraph
from .redaction import RectIndex


def _text_page(scratch, block: Block, options: Options, source=None, obstacles=(), cancel=None):
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
    if not block.inline:
        from .math_text import unchanged_math
        if not unchanged_math(block.text, block.translation):
            block.note("译文改变了普通字体公式，保留原文，请核对公式。")
            return None
    local = local_obstacles(block, obstacles)
    if local is None:
        block.note("旋转或竖排区域与相邻内容相交，保留原文，请调整区域。")
        return None
    page = scratch.new_page(width=width, height=height)
    font = max(3.0, block.fontsize)
    minimum = min(font, options.minimum_font)
    # insert_htmlbox injects `body {margin:1px}`. A universal selector cannot
    # override its specificity; reset body explicitly for dense table cells.
    css = "* {margin:0;padding:0;} body {margin:0;padding:0;font-family:sans-serif;} p {margin:0;line-height:1.08;}"
    archive = None
    if options.font_path:
        font_file = Path(options.font_path).resolve()
        archive = fitz.Archive(str(font_file.parent))
        css += f"@font-face {{font-family:custom;src:url('{font_file.name}');}} body {{font-family:custom;}}"
    if block.vertical:
        scale = vertical_paragraph(page, block, css, archive, minimum, font, source, cancel)
        return None if scale is None else (page.number, angle, scale)
    value = html.escape(block.translation)
    value = value.replace("\n", "<br/>")
    color = f"#{block.color & 0xFFFFFF:06x}"
    weight = "bold" if block.bold else "normal"
    markup = f'<p style="font-size:{font:.3f}pt;color:{color};font-weight:{weight}">{value}</p>'
    if block.inline or local:
        scale = flow_paragraph(page, block, css, archive, minimum, font, source, local, cancel)
        return None if scale is None else (page.number, angle, scale)
    spare, scale = page.insert_htmlbox(page.rect, markup, css=css, archive=archive, scale_low=max(0.05, minimum / font))
    if spare < 0:
        return None
    return page.number, angle, scale


def _obstacles(block, blocks, graphics):
    """Reserve pictures and actual neighbouring glyphs, not their empty gaps."""
    rect = fitz.Rect(block.rect)
    margin = .15 if block.role == "formula_label" else 1.5
    result = []
    for graphic in graphics:
        g = fitz.Rect(graphic)
        # Figure labels are intentionally replaced inside their own figure.
        if (block.role == "figure_label" or block.kind == "raster") and g.contains(rect):
            continue
        expanded = g + (-margin, -margin, margin, margin)
        if expanded.intersects(rect):
            result.append(expanded)
    for other in blocks:
        if other is block or not (fitz.Rect(other.rect) + (-margin, -margin, margin, margin)).intersects(rect):
            continue
        for value in other.glyphs or [other.rect]:
            g = fitz.Rect(value) + (-.15, -.15, .15, .15)
            if g.intersects(rect):
                result.append(g)
    return result


def _placement_mask(scratch, block, placement, clip):
    """Visible ink in a shared page-space clip, including formula clipping."""
    with fitz.open() as probe:
        page = probe.new_page(width=clip.width, height=clip.height)
        target = fitz.Rect(block.rect) - (clip.x0, clip.y0, clip.x0, clip.y0)
        page.show_pdf_page(target, scratch, placement[0], rotate=placement[1], keep_proportion=False)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=True)
        return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 4)[:, :, 3] > 0


def _conflicting_placements(prepared, scratch, cancel):
    ordered = sorted(prepared, key=lambda item: item[0].rect[1])
    conflicts = set()
    for i, (block, placement, _) in enumerate(ordered):
        checkpoint(cancel)
        for other, other_placement, _ in ordered[i + 1:]:
            if other.rect[1] >= block.rect[3]:
                break
            clip = fitz.Rect(block.rect) & fitz.Rect(other.rect)
            if clip.width < .25 or clip.height < .25:
                continue
            if np.any(_placement_mask(scratch, block, placement, clip) &
                      _placement_mask(scratch, other, other_placement, clip)):
                conflicts.update((block.id, other.id))
    return conflicts


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
    # Embed color and opacity separately. Decoding an RGBA PNG premultiplies
    # transparent pixels; resampling that color plane can produce gray glyph
    # outlines at higher zoom even though the 300-DPI preview looks clean.
    ok, encoded = cv2.imencode(".png", repaired)
    mask_ok, encoded_mask = cv2.imencode(".png", mask)
    if not ok or not mask_ok:
        return None, "图像修补编码失败。"
    # Use the exact pixel extent as the insertion rectangle to prevent subpixel
    # stretching when PDF clip coordinates were rounded by the rasterizer.
    actual_rect = fitz.Rect(pix.x / zoom, pix.y / zoom, (pix.x + pix.width) / zoom, (pix.y + pix.height) / zoom)
    return (actual_rect, encoded.tobytes(), encoded_mask.tobytes()), ""


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
    options.validate()
    selected = parse_pages(options.page_range, len(project.pages))
    if any(i not in project.selected_pages and not project.pages[i].analyzed for i in selected):
        raise ValueError('所选页码包含尚未分析的页面，请先分析版面后再导出。')
    project.selected_pages = selected
    project.options['page_range'] = options.page_range
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
        from .layout_upgrade import refresh_layout
        refresh_layout(project, document, cancel, log)
        for block in project.blocks:
            if block.page in project.selected_pages:
                block.clear_export_notes()
        # PDF graft maps snapshot the source object table. Build all text pages
        # before the first import so later additions cannot invalidate that map.
        text_prepared = {}
        text_errors = {}
        geometry = {}
        from .structure import figure_rects
        for index in project.selected_pages:
            checkpoint(cancel)
            original = document[index]
            rotation = original.rotation
            original.set_rotation(0)
            try:
                graphics = figure_rects(original)
                # Even small photographs / icons are obstacles. A full-page
                # scan is the page background, not an obstacle to its OCR text.
                graphics.extend(fitz.Rect(i["bbox"]) for i in original.get_image_info()
                                if .0001 < fitz.Rect(i["bbox"]).get_area() / original.rect.get_area() < .70)
                geometry[index] = ([b for b in project.blocks if b.page == index], graphics)
            finally:
                original.set_rotation(rotation)
        candidates = [b for b in project.blocks if b.page in project.selected_pages and not b.protected
                      and b.translation.strip() and b.translation != b.text and not b.layout_stale
                      and b.status not in ("preserved", "layout_pending") and block_enabled(b, options)]
        log(f"开始预排版 {len(candidates)} 个译文区域。")
        for number, block in enumerate(candidates):
            checkpoint(cancel)
            step("typeset", "预排版译文", number, len(candidates), block.page, block.id)
            blocks, graphics = geometry[block.page]
            before_notes = set(block.notes)
            text_prepared[block.id] = _text_page(scratch, block, options, document,
                                                _obstacles(block, blocks, graphics), cancel)
            text_errors[block.id] = bool(set(block.notes) - before_notes)
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
            protected_rects = [fitz.Rect(r) for b in blocks if b.protected for r in b.glyphs or [b.rect]]
            protected_index = RectIndex(protected_rects)
            page_bounds = page.rect
            for number, block in enumerate(blocks):
                checkpoint(cancel)
                if not block_enabled(block, options):
                    continue
                step("check_regions", "检查区域与图像背景", number, len(blocks), index, block.id)
                if block.notes and not block.approved:
                    unresolved.append(block.id)
                if block.protected or block.status == "preserved" or block.translation == block.text:
                    continue
                if block.layout_stale or block.status == "layout_pending":
                    block.note("公式或段落结构已更新；旧译文保留供核对，请重新翻译或人工确认后导出。")
                    unresolved.append(block.id)
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
                if block.kind == "native" and any(protected_index.intersects(e) for e in block.erasers):
                    block.note("文字与受保护公式重叠，保留原文，需人工调整区域。")
                    unresolved.append(block.id)
                    continue
                fit = text_prepared.get(block.id)
                if fit is None:
                    block.status = "overflow"
                    if not text_errors.get(block.id):
                        block.note(f"译文在原区域内无法以最低 {min(options.minimum_font, block.fontsize):g} pt 完整放下；保留原文。")
                    unresolved.append(block.id)
                    continue
                patch = None
                if block.kind == "raster":
                    # A broad OCR paragraph patch must never inpaint a photo
                    # that lies in a wraparound gap. Without individual OCR
                    # line masks, leave this ambiguous source region intact.
                    if any(fitz.Rect(g).intersects(rect) and not fitz.Rect(g).contains(rect)
                           for g in geometry[index][1]):
                        block.note("扫描文字区域跨越图片边界，保留原图，请按文字行调整区域。")
                        unresolved.append(block.id)
                        continue
                    step("repair_image", "修补图内文字", page=index, region=block.id)
                    patch, reason = _raster_patch(page, block, block.approved)
                    if patch is None:
                        block.note(reason)
                        unresolved.append(block.id)
                        continue
                prepared.append((block, fit, patch))
            step("check_collisions", "检查译文、公式与图片的间距", page=index)
            conflicts = _conflicting_placements(prepared, scratch, cancel)
            for block, _, _ in prepared:
                if block.id in conflicts:
                    block.note("相邻译文排版重叠，保留原文，请调整区域。")
                    block.status = "overflow"
                    unresolved.append(block.id)
            prepared = [item for item in prepared if item[0].id not in conflicts]
            # Validate deletion on a copy before changing the real source page.
            # Fonts with nonstandard ascent / descent can survive center-point
            # redactions. Never overlay a translation onto surviving source ink.
            from .redaction import characters, erasers_for, deletion_failures
            chars = characters(page)
            plans = [(b, *erasers_for(b, chars)) for b, _, _ in prepared if b.erasers or b.kind == 'native']
            if plans:
                step('verify_erase', '校验原文字形删除', page=index)
                failures = deletion_failures(document, index, plans, _add_redaction_quads, cancel)
                for b, _, _ in prepared:
                    if b.id in failures:
                        b.note('原文字形删除校验未通过，保留原文，请核对该区域。')
                        b.status = 'overflow'
                        unresolved.append(b.id)
                prepared = [item for item in prepared if item[0].id not in failures]
                plans = [item for item in plans if item[0].id not in failures]
            # Prepare all image patches from the untouched source page first.
            erasers = [eraser for _, boxes, _ in plans for eraser in boxes]
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
                    page.insert_image(patch[0], stream=patch[1], mask=patch[2], overlay=True)
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
