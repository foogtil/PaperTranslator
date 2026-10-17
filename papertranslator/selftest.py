from __future__ import annotations

import json
from collections import Counter
import os
from pathlib import Path
import threading
import traceback


def make_fixture(path: Path):
    import pymupdf as fitz
    with fitz.open() as doc:
        page = doc.new_page(width=600, height=780)
        page.insert_text((35, 32), "Synthetic fixture / offline transport test", fontsize=9, fontname="cour")
        page.insert_textbox((40, 60, 560, 105), "Computer science and mathematics", fontsize=18, fontname="hebo")
        page.insert_textbox((40, 120, 285, 215), "We study graph algorithms with 12 vertices. The method achieves 95.5% accuracy. See [1] for the proof.", fontsize=11)
        page.insert_textbox((315, 120, 560, 215), "The training process is stable. The numerical results are shown in the following table.", fontsize=11)
        page.insert_text((40, 225), "Consider", fontsize=11)
        page.insert_text((90, 225), "a", fontname="symb", fontsize=11)
        page.insert_text((104, 225), "in this proof.", fontsize=11)
        page.insert_text((40, 252), "x = 12; f(x) = x^2 + 1", fontname="cour", fontsize=12)
        for y in (290, 320, 350):
            page.draw_line((40, y), (560, y), width=0.5)
        for x in (40, 300, 560):
            page.draw_line((x, 290), (x, 350), width=0.5)
        for point, text in [((50, 309), "Algorithm"), ((310, 309), "Accuracy"), ((50, 339), "Graph search"), ((310, 339), "95.5%")]:
            page.insert_text(point, text, fontsize=11)
        page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(40, 120, 285, 160), "uri": "https://example.org/paper"})
        page = doc.new_page(width=780, height=500)
        page.insert_text((35, 35), "Landscape page", fontsize=15)
        page.insert_text((80, 300), "Training accuracy", fontsize=13, rotate=90)
        page.insert_text((420, 170), "Rotated label", fontsize=13, rotate=180)
        page.insert_text((700, 100), "Validation loss", fontsize=13, rotate=270)
        page.insert_text((260, 300), "Oblique label", fontsize=14, morph=(fitz.Point(260, 300), fitz.Matrix(23)))
        page.draw_rect((140, 60, 650, 420), color=(0.1, 0.2, 0.7), width=0.6)
        page = doc.new_page(width=460, height=640)
        page.insert_textbox((40, 60, 420, 150), "A page with PDF rotation. Preserve its orientation and dimensions.", fontsize=14)
        page.set_rotation(90)
        page = doc.new_page(width=600, height=780)
        page.insert_textbox((70, 100, 530, 190), "A cropped page keeps the original crop box. References and captions are translated.", fontsize=12)
        page.set_cropbox(fitz.Rect(20, 30, 580, 750))
        doc.set_toc([[1, "Computer science", 1], [1, "Landscape", 2]])
        doc.save(path)


def mock_transport(request):
    import httpx
    from .translation import PLACEHOLDER
    data = json.loads(request.content)
    messages = data["messages"]
    if messages[-1]["content"] == "Reply with the single word OK.":
        answer = "OK"
    else:
        content = messages[-1]["content"]
        if isinstance(content, list):
            content = next(item["text"] for item in content if item.get("type") == "text")
        body = json.loads(content)
        segments = []
        for segment in body["segments"]:
            # These are test strings, deliberately not a production translator.
            source = segment["source"]
            translation = "计算机科学与数学。" if len(source) > 50 else "计算方法"
            if "accuracy" in source.lower():
                translation = "准确率"
            if "landscape" in source.lower():
                translation = "横向页面"
            if "label" in source.lower():
                translation = "旋转标签"
            translation += " ".join(PLACEHOLDER.findall(source))
            segments.append({"id": segment["id"], "translation": translation, "preserve": False})
        answer = json.dumps({"segments": segments}, ensure_ascii=False)
    stream = "data: " + json.dumps({"choices": [{"delta": {"content": answer}, "finish_reason": None}]}, ensure_ascii=False) + "\n\n"
    stream += "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 80}}) + "\n\ndata: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=stream)


def run_checks(directory: str | Path, include_ocr=True, include_gui=True) -> dict:
    import httpx
    import pymupdf as fitz
    from .api import ChatClient, authentication_headers
    from .extraction import OCR, analyze, native_blocks
    from .layout import export_pdf
    from .models import Block, Options, Project, Provider, atomic_json
    from .translation import protect, restore, translate_project

    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    source, output = root / "synthetic.pdf", root / "synthetic.translated.pdf"
    path = root / "synthetic.ptrans.json"
    # Only fixtures created here are replaced; user PDFs are never touched.
    make_fixture(source)
    if path.exists():
        path.unlink()
    options = Options(ocr=False, vision_review=False)
    project = analyze(str(source), options, str(path), threading.Event(), lambda t: None)
    result = {"native_pages": len(project.pages), "native_regions": len(project.blocks), "checks": {}}
    invalid_key = "offline-selftest-key中文"
    try:
        authentication_headers(Provider(api_key=invalid_key))
        result["checks"]["api_header_validation"] = False
    except ValueError as error:
        result["checks"]["api_header_validation"] = "API Key" in str(error) and invalid_key not in str(error)
    client = ChatClient(Provider(api_key="offline-selftest-key"), transport=httpx.MockTransport(mock_transport))
    translate_project(project, options, client, lambda: project.save(path), lambda t: None)
    report = export_pdf(project, output, options)
    client.close()
    with fitz.open(source) as original, fitz.open(output) as translated:
        result["checks"]["page_count"] = len(original) == len(translated)
        result["checks"]["geometry_rotation_cropbox"] = all(a.mediabox == b.mediabox and a.cropbox == b.cropbox and a.rotation == b.rotation for a, b in zip(original, translated))
        result["checks"]["formulas_numbers_preserved"] = "x = 12; f(x) = x^2 + 1" in translated[0].get_text() and "95.5%" in translated[0].get_text()
        result["checks"]["searchable_chinese"] = "计算" in "".join(p.get_text() for p in translated)
        result["checks"]["bookmarks"] = original.get_toc() == translated.get_toc()
        result["checks"]["links"] = len(original[0].get_links()) == len(translated[0].get_links())
        result["checks"]["native_text_removed"] = "Computer science and mathematics" not in translated[0].get_text()
        # Inline vector formulas use clipped PDF forms. MuPDF enumerates paths
        # outside those clips too, so raw drawing counts may legitimately rise.
        # Verify every original path, style and coordinate remains present.
        def drawing_signature(drawing):
            return repr((drawing["items"], drawing.get("type"), drawing.get("width"), drawing.get("color"), drawing.get("fill")))
        original_paths = Counter(drawing_signature(d) for d in original[0].get_drawings())
        translated_paths = Counter(drawing_signature(d) for d in translated[0].get_drawings())
        result["checks"]["vector_graphics_preserved"] = all(translated_paths[key] >= count for key, count in original_paths.items())
        result["checks"]["inline_formula_objects"] = (any(b.inline and b.status == "translated" for b in project.blocks)
            and "MATH_" not in translated[0].get_text() and any("Symbol" in row[3] for row in translated[0].get_fonts(full=True)))
        for index in range(len(translated)):
            translated[index].get_pixmap(dpi=100).save(root / f"page-{index + 1}.png")
    cached = Project.load(path)
    result["checks"]["resume_project"] = len(cached.blocks) == len(project.blocks) and all(b.translation for b in cached.blocks)
    result["checks"]["no_secrets_in_project"] = "offline-selftest-key" not in path.read_text(encoding="utf-8")
    protected, tokens = protect("Use 12 values, 95.5%, [1] and $x^2$.")
    result["checks"]["protected_tokens_roundtrip"] = restore(protected, tokens) == "Use 12 values, 95.5%, [1] and $x^2$."
    calls = []
    def partial_response(request):
        body = json.loads(request.content)
        segments = json.loads(body["messages"][-1]["content"])["segments"]
        calls.append([s["id"] for s in segments])
        values = [{"id": s["id"], "translation": "" if len(calls) == 1 and s["id"] == "b1" else "测试译文"} for s in segments]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"segments": values}, ensure_ascii=False)}, "finish_reason": "stop"}]})
    partial = Project(str(source), project.source_hash, vars(options), blocks=[Block(f"b{i}", 0, [10, 10, 200, 30], f"Scientific phrase {chr(65 + i)}") for i in range(3)])
    partial_client = ChatClient(Provider(api_key="offline-selftest-key"), transport=httpx.MockTransport(partial_response))
    translate_project(partial, options, partial_client, lambda: None, lambda t: None)
    partial_client.close()
    result["checks"]["partial_translation_retry"] = calls == [["b0", "b1", "b2"], ["b1"]] and all(b.status == "translated" for b in partial.blocks)
    result["unresolved_regions"] = report["needs_review"]
    if include_ocr:
        with fitz.open() as chart:
            p = chart.new_page(width=340, height=190)
            p.insert_text((28, 45), "Training accuracy", fontsize=19)
            p.insert_text((28, 95), "Graph algorithm", fontsize=18)
            p.insert_text((28, 145), "95.5%", fontsize=17)
            image = p.get_pixmap(dpi=180).tobytes("png")
            with fitz.open() as scanned:
                p2 = scanned.new_page(width=340, height=190)
                p2.insert_image(p2.rect, stream=image)
                blocks = OCR().recognize(p2, 0, [], Options(ocr_dpi=180, ocr_rotations=True), threading.Event(), lambda t: None)
                result["ocr_text"] = [b.text for b in blocks]
                result["checks"]["offline_ocr_models"] = any("training" in b.text.lower() or "accuracy" in b.text.lower() for b in blocks)
                scan_path = root / "scanned.pdf"
                scanned.save(scan_path)
        from .models import PageInfo, digest_file
        from .selftest import mock_transport as transport
        scan_project = Project(str(scan_path), digest_file(scan_path), vars(options), pages=[PageInfo(0, 340, 190, 0)], blocks=blocks, selected_pages=[0])
        scan_client = ChatClient(Provider(api_key="offline-selftest-key"), transport=httpx.MockTransport(transport))
        translate_project(scan_project, options, scan_client, lambda: None, lambda t: None)
        scan_report = export_pdf(scan_project, root / "scanned.translated.pdf", options)
        scan_client.close()
        with fitz.open(root / "scanned.translated.pdf") as scanned:
            result["checks"]["raster_translation_searchable"] = "准确率" in scanned[0].get_text()
            scanned[0].get_pixmap(dpi=150).save(root / "scanned.translated.png")
        result["raster_unresolved"] = scan_report["needs_review"]
    if include_gui:
        import tkinter as tk
        from .gui import App, make_root
        app_root = make_root()
        app_root.withdraw()
        app = App(app_root)
        app._accept_paths([str(source)])
        app._show_project(project, path)
        app._options()
        app_root.update()
        def count_rows(parent=""):
            return sum(1 + count_rows(child) for child in app.block_tree.get_children(parent))
        figure_count = sum(u.kind == "figure" for u in project.units)
        result["checks"]["gui_widgets"] = len(app.file_tree.get_children()) == 1 and count_rows() == len(project.blocks) + figure_count
        result["checks"]["paragraph_figure_tree"] = len(app.block_tree.get_children()) == len(project.units)
        first_figure = next((u for u in project.units if u.kind == "figure"), None)
        if first_figure:
            app.block_tree.selection_set(first_figure.id)
            app._select_block()
            result["checks"]["whole_figure_overview"] = app._selected_unit().id == first_figure.id and app.source_text.cget("state") == "disabled"
        try:
            import tkinterdnd2
            result["checks"]["drag_and_drop"] = bool(app_root.tk.call("package", "present", "tkdnd"))
        except Exception:
            result["checks"]["drag_and_drop"] = False
        from .gui_checks import run_gui_checks
        result["checks"].update(run_gui_checks(app, project, path, root))
        result["gui_check_diagnostics"] = app.gui_check_diagnostics
        app._destroy()
    result["ok"] = all(result["checks"].values())
    return result


def self_test(report: str):
    from .models import atomic_json
    path = Path(report).resolve()
    # Self-test stores settings under its own output folder, never AppData.
    os.environ["PAPERTRANSLATOR_DATA_DIR"] = str(path.parent / "selftest-settings")
    try:
        result = run_checks(path.parent / "selftest-artifacts")
    except Exception:
        result = {"ok": False, "error": traceback.format_exc()}
    atomic_json(path, result)
    return result
