"""Offline GUI and spawned-worker checks, also available inside the frozen app."""
from __future__ import annotations

import queue
import copy
import threading
import time
import json
import os
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

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
    checks["checkbox_tick_and_states"] = (len(buttons) == 15 and all(toggled)
        and "PaperTranslator.Check.indicator" in repr(ttk.Style(root).layout("TCheckbutton")))
    labels = [str(w.cget("text")) for w in descendants(root) if w.winfo_class() == "Label"]
    checks["header_subtitle_removed"] = "PDF 原位翻译与校对" in labels and not any("计算机" in v and "数学" in v for v in labels)
    checks["opensource_notice"] = "PaperTranslator 已在 GitHub 上开源" in labels
    from . import PROJECT_URL
    from .localization import UI_LANGUAGES, Localizer, resolve_language
    from .settings import data_dir, save_ui_language
    from .gui import App, make_root
    with patch("papertranslator.gui.webbrowser.open") as opened:
        app._open_repository()
        checks["github_repository_link"] = (PROJECT_URL == "https://github.com/foogtil/PaperTranslator"
            and opened.call_args.args == (PROJECT_URL,) and bool(app.opensource_label.bind("<Button-1>"))
            and app.opensource_label.cget("cursor") == "hand2")
    checks["ui_language_default_and_coverage"] = (app.tr.language == "zh-CN"
        and app.ui_language_var.get() == "简体中文"
        and tuple(app.ui_language_combo.cget("values")) == tuple(UI_LANGUAGES.values()))
    root.update_idletasks()
    subtitle = app.ui_language_combo.master.master
    subtitle_label = subtitle.winfo_children()[0]
    checks["ui_language_control_position"] = (app.ui_language_combo.winfo_rooty() >=
        subtitle_label.winfo_rooty() + subtitle_label.winfo_height())
    original_preferences = (data_dir() / "settings.json").read_bytes() if (data_dir() / "settings.json").exists() else None
    original_language, profiles = app.ui_language, copy.deepcopy(app.profiles)
    document_languages = (app.source_var.get(), app.target_var.get())
    with patch("papertranslator.gui.messagebox.showinfo"):
        app.ui_language_var.set("English")
        app._change_ui_language()
    saved_preferences = json.loads((data_dir() / "settings.json").read_text(encoding="utf-8"))
    prior_preferences = json.loads(original_preferences) if original_preferences else {}
    checks["ui_language_preference_is_independent"] = (saved_preferences.pop("ui_language") == "en"
        and {k: v for k, v in prior_preferences.items() if k != "ui_language"} == saved_preferences
        and app.profiles == profiles and app.tr.language == "zh-CN"
        and (app.source_var.get(), app.target_var.get()) == document_languages)
    save_ui_language(original_language)
    app.ui_language, app.settings["ui_language"] = original_language, original_language
    app.ui_language_var.set(UI_LANGUAGES[original_language])
    language_results = []
    for code in UI_LANGUAGES:
        with patch.dict(os.environ, {"PAPERTRANSLATOR_DATA_DIR": str(Path(directory) / "locale-checks" / code)}):
            save_ui_language(code)
            localized_root = make_root()
            localized_root.withdraw()
            localized_app = App(localized_root)
            try:
                localized_root.update_idletasks()
                tr = Localizer(code)
                good = (localized_app.tr.language == resolve_language(code)
                    and localized_app.ui_language_var.get() == UI_LANGUAGES[code]
                    and localized_app.notebook.tab(0, "text") == tr("01  翻译队列")
                    and localized_app.analyze_button.cget("text") == tr("① 分析版面 / 估算费用")
                    and localized_app._options().source_language == "英语"
                    and localized_app._options().target_language == "简体中文")
                localized_app._show_project(project, path)
                block = project.blocks[0]
                localized_app.block_tree.selection_set(block.id)
                localized_app._select_block()
                good = good and localized_app.source_text.get("1.0", "end-1c") == block.text
                localized_app.provider_var.set("自定义")
                localized_app._switch_profile()
                good = good and localized_app.current_provider == "自定义"
                language_results.append(good)
            finally:
                localized_app._destroy()
    checks["all_ui_languages_start_and_keep_document_data"] = all(language_results) and len(language_results) == 13
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
    app.compact_context_var.set(False)
    app.translation_workers_var.set("3")
    app._save_all(show=False, validate=False)
    saved_context = load_settings()["options"]["compact_context"]
    checks["translation_concurrency_preference_persists"] = load_settings()["options"]["translation_workers"] == 3
    app.compact_context_var.set(True)
    app._load_options({"compact_context": False})
    checks["compact_context_preference_persists"] = saved_context is False and app._options().compact_context is False
    sample_usage = {"input_tokens": 123, "output_tokens": 45, "requests": 2,
                    "cached_input_tokens": 60, "cache_reported_requests": 1,
                    "reasoning_tokens": 20, "reasoning_reported_requests": 1,
                    "retry_requests": 1, "usage_missing_requests": 1,
                    "translation_input_tokens": 123, "translation_output_tokens": 45, "translation_requests": 2}
    app.events.put(("usage", sample_usage))
    # Drive the same bounded event loop that receives worker usage snapshots.
    root.update()
    deadline = time.monotonic() + 1
    while "123" not in app.usage_var.get() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    checks["usage_panel_worker_event"] = (all(value in app.usage_var.get() for value in ("123", "45", "60", "20", "168"))
        and app.usage_label.master is app.queue_actions)
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
