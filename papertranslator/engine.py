from __future__ import annotations

from collections import Counter
import math
from pathlib import Path
import threading

import pymupdf as fitz

from .api import APIError, ChatClient
from .extraction import OCR, analyze, coverage, crop_png
from .layout import export_pdf
from .models import Block, Options, Project, Provider, checkpoint, has_translation, block_enabled
from .translation import TOKEN, is_protected, translate_project
from .structure import assign_units, unit_counts
from .settings import data_dir


def project_path(source: str, output_dir: str) -> Path:
    from hashlib import sha256
    identity = sha256(str(Path(source).resolve()).encode()).hexdigest()[:8]
    return Path(output_dir) / (Path(source).stem + f".{identity}.paragraphs.ptrans.json")


def cached_project_path(source: str, output_dir: str) -> Path:
    from hashlib import sha256
    from .locking import identity
    key = sha256(identity(source).encode('utf-8')).hexdigest()[:20]
    return data_dir() / "jobs" / key / "project.ptrans.json"


def resume_project_path(source: str, output_dir: str, preferred: str | None = None) -> Path:
    """Use the explicitly opened project, or migrate an old output-scoped job."""
    from .locking import identity, job_lock
    with job_lock(source):
        cache = cached_project_path(source, output_dir)
        if preferred:
            selected = Path(preferred)
            project = Project.load(selected, verify=False)
            if identity(project.source) != identity(source):
                raise ValueError('已打开项目与当前原 PDF 不对应，请重新选择项目。')
            if identity(selected) != identity(cache):
                project.save(cache)
            return cache
        if cache.is_file():
            return cache
        legacy = project_path(source, output_dir)
        candidates = list((data_dir() / 'jobs').glob('*/project.ptrans.json'))
        if legacy.is_file():
            candidates.append(legacy)
        for path in sorted(candidates, key=lambda p: p.stat().st_mtime_ns, reverse=True):
            try:
                project = Project.load(path, verify=False)
                if identity(project.source) != identity(source):
                    continue
            except (ValueError, OSError, TypeError, KeyError):
                continue
            project.save(cache)
            break
        return cache


def estimate(project: Project, provider: Provider, options: Options) -> dict:
    active = [b for b in project.blocks if (not project.pages or b.page in project.selected_pages) and block_enabled(b, options)]
    count = sum(len(b.text) for b in active if not b.protected)
    regions = sum(not b.protected for b in active)
    counts = unit_counts(project)
    # Explicit estimates, never presented as measured token usage or a quote.
    input_tokens = int(count / 2.5 + regions * 90)
    output_tokens = int(count * (1.2 if "中文" in options.target_language else 0.7))
    cost = None
    if provider.input_price > 0 or provider.output_price > 0:
        cost = (input_tokens * provider.input_price + output_tokens * provider.output_price) / 1_000_000
    return {"regions": regions, "paragraphs": counts.get("paragraph", 0), "figures": counts.get("figure", 0), "characters": count, "input_tokens_estimated": input_tokens, "output_tokens_estimated": output_tokens,
            "estimated_text_cost": cost, "currency": provider.currency,
            "note": "粗略文本估算；不含视觉校对、重试、缓存差异及提供商推理 Token，实际以服务商账单为准。"}


class Engine:
    def __init__(self, log=None, cancel=None, progress=None, on_usage=None):
        self.log = log or (lambda text: None)
        self.cancel = cancel or threading.Event()
        self.progress = progress
        self.on_usage = on_usage or (lambda data: None)
        self.ocr = OCR()

    def analyze(self, source, options, path):
        return analyze(source, options, str(path), self.cancel, self.log, self.ocr)

    def translate(self, project: Project, options: Options, provider: Provider, path):
        state_lock = threading.RLock()
        def save():
            with state_lock:
                project.save(path)

        def usage(data):
            with state_lock:
                for key, value in data.items():
                    project.usage[key] = project.usage.get(key, 0) + value
                self.on_usage(dict(project.usage))

        self.on_usage(dict(project.usage))
        client = ChatClient(provider, self.cancel, on_usage=usage, on_log=self.log)
        try:
            if options.vision_review and provider.vision:
                with fitz.open(project.source) as document:
                    vision_failed = False
                    for index in project.selected_pages:
                        if project.pages[index].vision_analyzed:
                            continue
                        checkpoint(self.cancel)
                        page = document[index]
                        rotation = page.rotation
                        page.set_rotation(0)
                        try:
                            width, height = page.rect.width, page.rect.height
                            from .structure import figure_rects, inside
                            figures = figure_rects(page)
                            existing_blocks = [b for b in project.blocks if b.page == index]
                            existing = [{"text": b.text[:160], "bbox": [b.rect[0] / width * 1000, b.rect[1] / height * 1000, b.rect[2] / width * 1000, b.rect[3] / height * 1000]} for b in existing_blocks]
                            self.log(f"视觉检查第 {index + 1} 页的漏识别、旋转及竖排文字。")
                            pix = page.get_pixmap(matrix=fitz.Matrix(min(220 / 72, 2400 / max(width, height)), min(220 / 72, 2400 / max(width, height))), alpha=False)
                            if not options.translate_figures:
                                import io
                                from PIL import Image
                                from .extraction import mask_figures
                                image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
                                mask_figures(image, figures, min(220 / 72, 2400 / max(width, height)))
                                stream = io.BytesIO()
                                image.save(stream, format='PNG')
                                png = stream.getvalue()
                            else:
                                png = pix.tobytes('png')
                            result = client.read_layout(png, existing)
                            for region in result["regions"][:500]:
                                if not isinstance(region, dict) or not isinstance(region.get("text"), str):
                                    continue
                                text = region["text"].strip()
                                box = region.get("bbox")
                                if not text or is_protected(text) or not isinstance(box, list) or len(box) != 4:
                                    continue
                                try:
                                    box = [float(v) for v in box]
                                    angle, confidence = float(region.get("angle", 0)) % 360, float(region.get("confidence", 0))
                                except (ValueError, TypeError):
                                    continue
                                if not all(math.isfinite(v) for v in box + [angle, confidence]) or not (0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 1000):
                                    continue
                                rect = [box[0] * width / 1000, box[1] * height / 1000, box[2] * width / 1000, box[3] * height / 1000]
                                if not options.translate_figures and any(inside(rect, f) for f in figures):
                                    continue
                                if any(coverage(rect, b.rect) > 0.55 for b in project.blocks if b.page == index):
                                    continue
                                rw, rh = rect[2] - rect[0], rect[3] - rect[1]
                                local_w, local_h = (rh, rw) if round(angle) % 180 == 90 else (rw, rh)
                                vertical = region.get("vertical") is True
                                if vertical:
                                    angle = 0
                                    local_w, local_h = rw, rh
                                number = 1 + sum(b.page == index and "-v" in b.id for b in project.blocks)
                                block = Block(id=f"p{index + 1}-v{number}", page=index, rect=rect, text=text, kind="raster", angle=angle,
                                              vertical=vertical, confidence=max(0, min(1, confidence)), fontsize=min(14, (rw if vertical else local_h) * 0.75),
                                              quad=[[rect[0], rect[1]], [rect[2], rect[1]], [rect[2], rect[3]], [rect[0], rect[3]]],
                                              local_width=local_w, local_height=local_h)
                                block.note("由视觉模型补充定位，请核对原文、位置及方向。")
                                project.blocks.append(block)
                            project.pages[index].vision_analyzed = True
                            if project.extraction_version >= 3:
                                assign_units(project)
                            save()
                        except APIError as error:
                            self.log(f"视觉定位暂停，继续本地识别结果：{error}")
                            warning = "视觉补充定位未完成；部分图片或非中英文文字可能需要框选补充。"
                            if warning not in project.warnings:
                                project.warnings.append(warning)
                            vision_failed = True
                            break
                        finally:
                            page.set_rotation(rotation)
                    for b in project.blocks:
                        if vision_failed:
                            break
                        if b.page not in project.selected_pages or b.kind != "raster" or b.role == "figure_label" or b.protected or has_translation(b):
                            continue
                        if b.confidence >= 0.96 and not b.notes:
                            continue
                        checkpoint(self.cancel)
                        page = document[b.page]
                        rotation = page.rotation
                        page.set_rotation(0)
                        try:
                            self.log(f"视觉核对 {b.id} 的图中文字。")
                            result = client.read_crop(crop_png(page, b), b.text)
                            text = result.get("text")
                            if not isinstance(text, str) or not text.strip():
                                b.note("视觉校对没有读出文字，请人工核对。")
                                continue
                            if result.get("uncertain", True):
                                b.note("视觉模型对图片文字不确定，请人工核对。")
                            else:
                                if Counter(TOKEN.findall(text)) != Counter(TOKEN.findall(b.text)):
                                    b.note("视觉核对改变了数字或标识，请核对原图。")
                                b.text = text.strip()
                                if result.get("vertical") is True:
                                    b.vertical = True
                                    b.angle = 0
                                elif isinstance(result.get("angle"), (int, float)) and math.isfinite(result["angle"]):
                                    b.angle = float(result["angle"]) % 360
                            if result.get("is_formula") is True or is_protected(b.text):
                                b.protected = True
                                b.status = "preserved"
                                b.translation = b.text
                            save()
                        except APIError as error:
                            b.note("视觉校对不可用，采用本地 OCR，需人工核对。")
                            self.log(f"视觉校对暂停：{error}")
                            project.warnings.append("视觉校对未完成；请检查模型图片能力及接口设置。")
                            vision_failed = True
                        finally:
                            page.set_rotation(rotation)
            translate_project(project, options, client, save, self.log, state_lock=state_lock)
        finally:
            client.close()
            save()

    def export(self, project, output, options, path):
        # An interrupted export must not require another paid translation.
        project.save(path)
        report = export_pdf(project, output, options, self.cancel, self.log, self.progress)
        project.save(path)
        return report
