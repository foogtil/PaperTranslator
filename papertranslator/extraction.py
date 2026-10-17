from __future__ import annotations

import io
import math
import re
from pathlib import Path
import statistics
import threading

import numpy as np
from PIL import Image
import pymupdf as fitz

from .models import Block, Options, PageInfo, Project, checkpoint, digest_file, parse_pages
from .translation import is_protected
from .structure import assign_units, figure_rects, join_continuations, merge_ocr_paragraphs, paragraph_runs, paragraph_template


def coverage(first, second):
    a, b = fitz.Rect(first), fitz.Rect(second)
    intersection = a & b
    return max(0, intersection.get_area()) / max(0.001, min(a.get_area(), b.get_area()))


def _center_erasers(spans):
    result = []
    for span in spans:
        for char in span.get("chars", []):
            if char["c"].isspace():
                continue
            x0, y0, x1, y1 = char["bbox"]
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            result.append([cx - 0.025, cy - 0.025, cx + 0.025, cy + 0.025])
    return result


LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})


def _hard_protected(span, text: str, line_size: float) -> bool:
    font = span.get("font", "").lower()
    flags = span.get("flags", 0)
    if flags & 8 or any(name in font for name in ("cmmi", "cmsy", "cmex", "math", "symbol", "msam", "msbm")):
        return True
    value = text.strip()
    if span["size"] < line_size * 0.80 and re.fullmatch(r"[\d,;\[\]()\s]+", value):
        return True  # Preserve superscript citation / affiliation placement.
    if flags & 2 and re.fullmatch(r"[A-Za-z](?:[_^][\w{}]+)?", value):
        return True
    if font.endswith(".i") and re.fullmatch(r"[A-Za-z]{1,3}", value) and value.lower() not in ("the", "and", "for", "of", "in", "to", "as", "is", "an", "at", "on", "by", "be", "or", "if", "so", "we"):
        return True
    return is_protected(value) and any(c in "=∑∫√∞≤≥≠∂∇⊂⊆∈∉∪∩∀∃⇒↦±×÷^{}" or "α" <= c <= "ω" for c in value)


def _span_projection(line, span):
    quad = fitz.recover_quad(tuple(line["dir"]), span)
    dx, dy = line["dir"]
    points = (quad.ul, quad.ur, quad.lr, quad.ll)
    along = [p.x * dx + p.y * dy for p in points]
    down = [-p.x * dy + p.y * dx for p in points]
    return min(along), max(along), min(down), max(down)


def _run_text(group):
    result, previous = "", None
    for line, span, value, hard in group:
        value = value if hard else value.translate(LIGATURES)
        if previous is not None:
            old_line, old_span, _, _ = previous
            different_row = (line is not old_line and (abs(line["dir"][0] - 1) > 0.01 or
                             abs(span.get("origin", (0, 0))[1] - old_span.get("origin", (0, 0))[1]) > max(span["size"], old_span["size"]) * 0.7))
            if different_row:
                result = result.rstrip() + "\n"
            elif result and value and not result[-1].isspace() and not value[0].isspace():
                gap = _span_projection(line, span)[0] - _span_projection(line, old_span)[1]
                if gap > min(span["size"], old_span["size"]) * 0.12 and value[0] not in ",.;:!?)]}" and result[-1] not in "([{":
                    result += " "
        result += value
        previous = (line, span, value, hard)
    result = result.replace("\u00ad", "")
    return re.sub(r"(?<=\w)-\n(?=[a-z])", "", result).replace("\n", " ").strip()


def native_blocks(page, page_index: int, figures=None) -> list[Block]:
    figures = figure_rects(page) if figures is None else figures
    data = page.get_text("rawdict", flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)
    invisible = [fitz.Rect(t["bbox"]) for t in page.get_texttrace() if t.get("type") == 3 or t.get("opacity", 1) < 0.05]
    output = []
    for raw in data["blocks"]:
        if raw.get("type") != 0:
            continue
        line_groups = []
        lines = raw.get("lines", [])
        for line in lines:
            spans = [s for s in line["spans"] if any(not c["c"].isspace() for c in s.get("chars", []))]
            if not spans:
                continue
            typical = max(spans, key=lambda s: len(s.get("chars", [])))["size"]
            groups, group = [], []
            for span in spans:
                text = "".join(c["c"] for c in span["chars"])
                hard = _hard_protected(span, text, typical)
                if group:
                    previous = group[-1]
                    gap = _span_projection(line, span)[0] - _span_projection(line, previous[1])[1]
                    separate = hard != previous[3] or gap > max(span["size"], previous[1]["size"]) * 1.8
                    if separate:
                        groups.append(group)
                        group = []
                group.append((line, span, text, hard))
            if group:
                groups.append(group)
            line_groups.append(groups)
        if not line_groups:
            continue
        all_runs = [r for groups in line_groups for g in groups for r in g]
        dirs = {tuple(round(v, 3) for v in r[0]["dir"]) for r in all_runs}
        typical = statistics.median(r[1]["size"] for r in all_runs)
        left_edges = [groups[0][0][0]["bbox"][0] for groups in line_groups]
        y_positions = [groups[0][0][0]["bbox"][1] for groups in line_groups]
        aligned = max(left_edges) - min(left_edges) < typical * 2.5
        nearby = all(abs(b - a) < typical * 2.1 for a, b in zip(y_positions, y_positions[1:]))
        merge = (len(dirs) == 1 and aligned and nearby and all(len(g) == 1 for g in line_groups)
                 and not any(r[3] or r[0].get("wmode") for r in all_runs))
        groups = [all_runs] if merge else [g for line in line_groups for g in line]
        paragraphs = paragraph_runs(raw, _hard_protected, _run_text, figures)
        if paragraphs is not None:
            groups = paragraphs
        for group in groups:
            line = group[0][0]
            span = max(group, key=lambda r: len(r[2]))[1]
            text = _run_text(group)
            if not text:
                continue
            keep = all(r[3] for r in group) or is_protected(text)
            if (not re.search(r"[a-z]{3,}|[\u3400-\u9fff]{2,}", text) and
                (any(r[3] for r in group) or re.search(r"[=∑∫√∞≤≥≠]|^[ðÞ\d()]+$", text))):
                keep = True
            rect = fitz.Rect(group[0][1]["bbox"])
            for run in group[1:]:
                rect |= fitz.Rect(run[1]["bbox"])
            dx, dy = line["dir"]
            angle = (-math.degrees(math.atan2(dy, dx))) % 360
            limits = [_span_projection(ln, sp) for ln, sp, _, _ in group]
            left, right = min(v[0] for v in limits), max(v[1] for v in limits)
            top, bottom = min(v[2] for v in limits), max(v[3] for v in limits)
            quad = [[a * dx - b * dy, a * dy + b * dx] for a, b in ((left, top), (right, top), (right, bottom), (left, bottom))]
            block = Block(id=f"p{page_index + 1}-n{len(output) + 1}", page=page_index, rect=list(rect), text=text,
                          fontsize=span["size"], color=span.get("color", 0), bold=bool(span.get("flags", 0) & 16 or span.get("font", "").lower().endswith(".b")),
                          angle=round(angle, 3), vertical=bool(line.get("wmode", 0)), quad=quad,
                          erasers=_center_erasers([r[1] for r in group]), protected=keep,
                          local_width=right - left, local_height=bottom - top)
            block.role = "protected" if keep else "paragraph"
            if not keep and any(r[3] for r in group) and abs(angle) < 0.01:
                block.template, block.inline = paragraph_template(group, _run_text)
            if keep:
                block.status, block.translation = "preserved", text
            if block.vertical:
                block.angle = 0
                block.local_width, block.local_height = rect.width, rect.height
                block.note("原文为竖排，请核对阅读顺序、标点及混合文字。")
            if any(coverage(block.rect, r) > 0.7 for r in invisible):
                block.kind, block.confidence = "raster", 0.85
                block.note("来自扫描件已有的 OCR 文字层，请对照原图核对。")
            output.append(block)
    return join_continuations(output, figures)


def inverse_rotated_point(x, y, width, height, rotation):
    # PIL rotates counter-clockwise, coordinates use a top-left origin.
    if rotation == 90:
        return width - y, x
    if rotation == 180:
        return width - x, height - y
    if rotation == 270:
        return y, height - x
    return x, y


class OCR:
    def __init__(self):
        self._engine = None

    @property
    def engine(self):
        if self._engine is None:
            import onnxruntime
            onnxruntime.disable_telemetry_events()
            from rapidocr_onnxruntime import RapidOCR
            self._engine = RapidOCR(det_model_path=None, det_limit_side_len=2200, det_limit_type="max", use_angle_cls=False)
        return self._engine

    def recognize(self, page, page_index, natives: list[Block], options: Options, cancel, log, clip=None) -> list[Block]:
        zoom = options.ocr_dpi / 72
        # Call only after normalizing page rotation to zero.
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, clip=clip)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        width, height = image.size
        rotations = (0, 90, 180, 270) if options.ocr_rotations else (0,)
        candidates = []
        for rotation in rotations:
            checkpoint(cancel)
            log(f"第 {page_index + 1} 页文字识别，方向 {rotation}°。")
            view = image.rotate(rotation, expand=True)
            # RapidOCR accepts BGR arrays; full-size page provides accurate boxes.
            result, _ = self.engine(np.asarray(view)[:, :, ::-1].copy())
            for polygon, text, confidence in result or []:
                if not text.strip():
                    continue
                points = [inverse_rotated_point(x, y, width, height, rotation) for x, y in polygon]
                points = [[(x + pix.x) / zoom, (y + pix.y) / zoom] for x, y in points]
                rect = [min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)]
                if any(coverage(rect, n.rect) > 0.65 for n in natives):
                    continue
                edge = np.array(points[1]) - np.array(points[0])
                down = np.array(points[3]) - np.array(points[0])
                local_w, local_h = float(np.linalg.norm(edge)), float(np.linalg.norm(down))
                # RapidOCR internally rotates tall detection crops 90° CCW.
                # Restore that implicit transform before mapping the baseline.
                if local_h / max(0.001, local_w) >= 1.5:
                    points = points[1:] + points[:1]
                    edge = np.array(points[1]) - np.array(points[0])
                    down = np.array(points[3]) - np.array(points[0])
                    local_w, local_h = float(np.linalg.norm(edge)), float(np.linalg.norm(down))
                angle = (-math.degrees(math.atan2(edge[1], edge[0]))) % 360
                # A horizontal label recognized in the wrong page rotation is
                # usually much less confident; deduplicate by geometric overlap.
                block = Block(id="", page=page_index, rect=rect, text=text.strip(), kind="raster",
                              confidence=float(confidence), fontsize=max(3, local_h * 0.78), angle=round(angle, 3),
                              quad=points, local_width=local_w, local_height=local_h)
                previous = next((b for b in candidates if coverage(rect, b.rect) > 0.65), None)
                if previous:
                    if block.confidence > previous.confidence + 0.015:
                        candidates.remove(previous)
                    else:
                        continue
                if is_protected(block.text):
                    block.protected = True
                    block.translation = block.text
                    block.status = "preserved"
                if block.confidence < 0.9:
                    block.note(f"OCR 置信度 {block.confidence:.0%}，请核对原文。")
                if rect[3] - rect[1] > (rect[2] - rect[0]) * 1.4 and sum("\u3400" <= c <= "\u9fff" for c in text) > len(text) * 0.5:
                    block.note("疑似竖排或旋转的汉字，请核对阅读方向。")
                candidates.append(block)
        candidates.sort(key=lambda b: (round(b.rect[1] / 4), b.rect[0]))
        for i, block in enumerate(candidates):
            block.id = f"p{page_index + 1}-o{i + 1}"
        return candidates


def crop_png(page, block: Block, dpi: int = 220):
    rect = fitz.Rect(block.rect) + (-1, -1, 1, 1)
    rect &= page.rect
    pix = page.get_pixmap(clip=rect, dpi=dpi, alpha=False)
    return pix.tobytes("png")


def analyze(source: str, options: Options, save_path: str, cancel: threading.Event, log, ocr: OCR | None = None, password: str = "") -> Project:
    options.validate()
    source = str(Path(source).resolve())
    project_path = Path(save_path)
    source_hash = digest_file(source)
    project = None
    if project_path.exists():
        candidate = Project.load(project_path)
        old = candidate.options
        extraction_keys = ("ocr", "ocr_dpi", "ocr_rotations", "page_range", "translate_figures")
        if candidate.source_hash == source_hash and all(old.get(k, True if k == "translate_figures" else None) == vars(options).get(k) for k in extraction_keys):
            project = candidate
            if project.extraction_version < 3:
                log("此项目沿用旧版分块并保留已有进度。若正文已被逐词切分，建议选择新的输出目录，用新版重新分析；原任务进度继续保留。")
    with fitz.open(source) as document:
        if document.needs_pass and not document.authenticate(password):
            raise ValueError("PDF 已加密。请先使用拥有权限的密码解锁后再导入。")
        selected = parse_pages(options.page_range, len(document))
        if project is None:
            project = Project(source=source, source_hash=source_hash, options=vars(options).copy(), selected_pages=selected,
                              pages=[PageInfo(i, float(p.mediabox.width), float(p.mediabox.height), p.rotation) for i, p in enumerate(document)], extraction_version=3)
        ocr = ocr or OCR()
        for index in selected:
            checkpoint(cancel)
            if project.pages[index].analyzed:
                log(f"第 {index + 1} 页使用已保存的分析结果。")
                continue
            page = document[index]
            rotation = page.rotation
            page.set_rotation(0)
            try:
                log(f"分析第 {index + 1}/{len(document)} 页。")
                figures = figure_rects(page)
                blocks = native_blocks(page, index, figures)
                if options.ocr:
                    blocks.extend(ocr.recognize(page, index, blocks, options, cancel, log))
                elif options.translate_figures:
                    for number, rect in enumerate(figures):
                        checkpoint(cancel)
                        log(f"第 {index + 1} 页整图 {number + 1}/{len(figures)}：识别图内标签。")
                        labels = ocr.recognize(page, index, blocks, options, cancel, log, clip=rect)
                        for n, block in enumerate(labels):
                            block.id = f"p{index + 1}-f{number + 1}-o{n + 1}"
                        blocks.extend(labels)
                elif not blocks:
                    project.warnings.append(f"第 {index + 1} 页未发现可提取文字；当前关闭了 OCR。")
                if not blocks:
                    project.warnings.append(f"第 {index + 1} 页未识别出文字，请检查预览或框选补充。")
                blocks = merge_ocr_paragraphs(blocks, figures)
                project.blocks = [b for b in project.blocks if b.page != index] + blocks
                existing_figures = {i: [u.rect for u in project.units if u.page == i and u.kind == "figure"] for i in selected}
                existing_figures[index] = [list(r) for r in figures]
                assign_units(project, existing_figures)
                project.pages[index].analyzed = True
            finally:
                page.set_rotation(rotation)
            project.save(save_path)
        project.save(save_path)
    return project
