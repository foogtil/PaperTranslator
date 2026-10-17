"""Offline GUI and spawned-worker checks, also available inside the frozen app."""
from __future__ import annotations

import queue
import copy
import threading
import time
from pathlib import Path
from tkinter import ttk

from .models import Options, Project
from .worker import PDFWorker, _MessageSender


def _stalled_export_fixture(messages, source, path, temporary):
    # Test only: simulate a native save call that never checks cancellation.
    Path(temporary).write_bytes(b"offline temporary PDF fixture")
    messages.put(("project_path", (source, path)))
    messages.put(("progress", {"category": "export", "phase": "save", "label": "离线停止测试",
                               "current": 0, "total": 0, "temporary": temporary}))
    time.sleep(30)


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def run_gui_checks(app, project, path, directory):
    from .gui import LOG_CHARACTERS, LOG_LINES

    checks, root = {}, app.root
    buttons = [w for w in descendants(root) if isinstance(w, ttk.Checkbutton)]
    toggled = []
    for button in buttons:
        before = root.getboolean(root.getvar(button.cget("variable")))
        button.invoke()
        toggled.append(root.getboolean(root.getvar(button.cget("variable"))) != before
                       and button.instate(["selected"]) != before)
        button.invoke()
        toggled.append(root.getboolean(root.getvar(button.cget("variable"))) == before)
    checks["checkbox_tick_and_states"] = (len(buttons) == 14 and all(toggled)
        and "PaperTranslator.Check.indicator" in repr(ttk.Style(root).layout("TCheckbutton")))
    labels = [str(w.cget("text")) for w in descendants(root) if w.winfo_class() == "Label"]
    checks["header_subtitle_removed"] = "PDF 原位翻译与校对" in labels and not any("计算机" in v and "数学" in v for v in labels)
    checks["opensource_notice"] = "PaperTranslator 已在 GitHub 上开源" in labels
    saved_profiles, saved_provider = copy.deepcopy(app.profiles), app.current_provider
    app.provider_var.set("DeepSeek")
    app._switch_profile()
    app.model_var.set("manual-deepseek-model")
    app.provider_var.set("MiMo")
    app._switch_profile()
    mimo = tuple(app.model_combo.cget("values"))
    app.model_var.set("manual-mimo-model")
    app.provider_var.set("DeepSeek")
    app._switch_profile()
    deepseek = tuple(app.model_combo.cget("values"))
    checks["provider_model_choices"] = (all(v.startswith("mimo-") for v in mimo)
        and all(v.startswith("deepseek-") for v in deepseek) and bool(mimo) and bool(deepseek)
        and app.model_var.get() == "manual-deepseek-model"
        and app.profiles["DeepSeek"]["name"] == "DeepSeek")
    app.profiles, app.current_provider = saved_profiles, saved_provider
    app.provider_var.set(saved_provider)
    app._load_profile(saved_provider)
    old_settings = copy.deepcopy(app.settings)
    app.review_marks_var.set(True)
    app.export_json_var.set(True)
    app._save_all(show=False, validate=False)
    from .settings import load_settings
    app.settings = load_settings()
    app.review_marks_var.set(False)
    app.export_json_var.set(False)
    app._load_options()
    app._load_options(project.options)
    checks["export_preferences_persist"] = app.review_marks_var.get() and app.export_json_var.get()
    app.settings = old_settings
    app._load_options()
    app._save_all(show=False, validate=False)
    app._render_preview()
    images = [str(photo) for photo in app.tk_images]
    for _ in range(100):
        app._render_preview()
    checks["preview_images_reused"] = images == [str(photo) for photo in app.tk_images] and all(app.tk_images)
    for number in range(2200):
        app._log(f"Offline UI log {number}")
    lines = int(app.log_text.index("end-1c").split(".")[0]) - 1
    app._log("x" * (LOG_CHARACTERS + 1000))
    checks["gui_log_memory_bounded"] = (lines <= LOG_LINES
        and app.log_text.count("1.0", "end-1c", "chars")[0] <= LOG_CHARACTERS)
    app.log_text.configure(state="normal")
    app.log_text.delete("1.0", "end")
    app.log_text.configure(state="disabled")
    root.update()
    app.gui_check_diagnostics = []

    def run_job(request, cancel=False, stalled=False):
        job = PDFWorker(request)
        if stalled:
            import multiprocessing
            job.process.close()
            job.process = multiprocessing.get_context("spawn").Process(
                target=_stalled_export_fixture,
                args=(_MessageSender(job.sender), project.source, str(path),
                      str(Path(directory) / ".papertranslator-offline-stop.pdf")), daemon=True)
        if cancel:
            job.cancel.set()
        job.start()
        messages = queue.Queue()
        reader = threading.Thread(target=job.relay, args=(messages,), daemon=True)
        reader.start()
        ticks, deadline = 0, time.monotonic() + 40
        while reader.is_alive() and time.monotonic() < deadline:
            root.update()
            if stalled and job.exporting and not job.forced:
                job.force_stop_export()
            ticks += 1
            time.sleep(0.005)
        if reader.is_alive():
            job.cancel.set()
            raise RuntimeError("Offline PDF worker did not finish in 40 seconds")
        result = []
        while not messages.empty():
            result.append(messages.get_nowait())
        app.gui_check_diagnostics.append({"kind": request["kind"], "cancel": cancel, "stalled_fixture": stalled,
                                          "ticks": ticks, "messages": [v for v in result if v[0] != "progress"],
                                          "progress_phases": sorted({v[1]["phase"] for v in result if v[0] == "progress"})})
        return result, ticks

    directory = Path(directory)
    from .engine import cached_project_path
    # The synthetic PDF is recreated on every self-test run; discard only this
    # test's own stale analysis cache, never a user's project.
    cached_project_path(project.source, str(directory / "worker-analysis")).unlink(missing_ok=True)
    options = vars(Options(ocr=False, translate_figures=False, vision_review=False))
    request = {"kind": "queue", "mode": "analyze", "files": [project.source],
               "output": str(directory / "worker-analysis"), "options": options,
               "provider": {}, "autoexport": False}
    messages, ticks = run_job(request)
    snapshots = [value for kind, value in messages if kind == "project_path"]
    checks["spawned_analysis_keeps_gui_alive"] = (ticks > 2 and bool(snapshots)
        and bool(Project.load(snapshots[0][1]).pages) and not any(kind == "error" for kind, _ in messages)
        and messages[-1][0] == "idle")
    messages, _ = run_job(request, cancel=True)
    checks["spawned_cancel"] = (any("已取消" in value for kind, value in messages if kind == "log")
        and not any(kind == "error" for kind, _ in messages) and messages[-1][0] == "idle")
    project.save(path)
    output = directory / "worker.translated.pdf"
    messages, ticks = run_job({"kind": "export", "project_path": str(path),
                               "output": str(output), "options": options})
    saved = Project.load(path)
    checks["spawned_export_keeps_progress"] = (ticks > 2 and output.is_file() and saved.output == str(output)
        and any(b.translation for b in saved.blocks) and not any(kind == "error" for kind, _ in messages))
    checks["pdf_only_export"] = not output.with_suffix(".report.json").exists() and not output.with_suffix(".ptrans.json").exists()
    phases = {value["phase"] for kind, value in messages if kind == "progress"}
    checks["export_phase_progress"] = {"typeset", "erase_text", "fonts", "save", "verify", "complete"} <= phases
    app._set_busy(True)
    app._export_update({"category": "export", "phase": "write_text", "label": "写回译文",
                        "current": 2, "total": 5, "page": 1})
    app.export_changed_at -= 61
    app._export_status()
    checks["progress_counter_and_stall_notice"] = (str(app.progress.cget("mode")) == "determinate"
        and float(app.progress.cget("value")) == 2 and "2/5" in app.status.get() and app.export_warned
        and "暂无新进展" in app.status.get())
    app._set_busy(False)
    marked = directory / "optional-review.pdf"
    saved.blocks[0].note("离线可选批注测试")
    saved.save(path)
    marked_options = options | {"pdf_review_marks": True, "export_json": True}
    messages, _ = run_job({"kind": "export", "project_path": str(path), "output": str(marked), "options": marked_options})
    import pymupdf as fitz
    with fitz.open(marked) as document:
        marks = any(a.info.get("title") == "PaperTranslator 校对提示" for p in document for a in p.annots() or [])
    checks["optional_review_marks_and_json"] = (marks and marked.with_suffix(".report.json").is_file()
        and Project.load(marked.with_suffix(".ptrans.json")).output == str(marked)
        and not any(kind == "error" for kind, _ in messages))
    before = Path(path).read_bytes()
    messages, _ = run_job(request, stalled=True)
    checks["forced_export_stop_preserves_cache"] = (Path(path).read_bytes() == before and output.is_file()
        and not (directory / ".papertranslator-offline-stop.pdf").exists()
        and any("已强制停止" in value for kind, value in messages if kind == "log")
        and not any(kind == "error" for kind, _ in messages) and messages[-1][0] == "idle")
    app.project = project
    return checks
