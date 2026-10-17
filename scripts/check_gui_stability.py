"""Offline mapped-window stress check. Uses only generated PDFs and fake settings."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def resources(root):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    process = kernel.GetCurrentProcess()
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    user.GetGuiResources.restype = wintypes.DWORD
    count = wintypes.DWORD()
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessHandleCount(process, ctypes.byref(count))
    return {"gdi": user.GetGuiResources(process, 0), "user": user.GetGuiResources(process, 1),
            "handles": count.value, "tk_images": len(root.tk.splitlist(root.tk.call("image", "names")))}


def main():
    import multiprocessing
    multiprocessing.freeze_support()
    import pymupdf as fitz
    from papertranslator import __version__
    from papertranslator.gui import App, make_root
    from papertranslator.engine import cached_project_path
    from papertranslator.models import Options
    from papertranslator.selftest import make_fixture

    directory = (ROOT / "test-output" / f"v{__version__}-stability").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    os.environ["PAPERTRANSLATOR_DATA_DIR"] = str(directory / "isolated-settings")
    source = directory / "synthetic.pdf"
    make_fixture(source)
    with fitz.open(source) as fixture, fitz.open() as combined:
        for _ in range(10):
            combined.insert_pdf(fixture)
        combined.save(directory / "forty-pages.pdf")
    for fixture in (source, directory / "forty-pages.pdf"):
        cached_project_path(str(fixture), str(directory)).unlink(missing_ok=True)
    root = make_root()
    root.geometry("1280x860")
    root.lower()
    app = App(root)
    errors, samples, gaps, ticks = [], [], [], []
    root.report_callback_exception = lambda *exc: errors.append(str(exc[1]))
    last, heartbeat_id = time.monotonic(), None

    def heartbeat():
        nonlocal last, heartbeat_id
        now = time.monotonic()
        gaps.append(now - last)
        last = now
        ticks.append(now)
        heartbeat_id = root.after(20, heartbeat)

    heartbeat_id = root.after(20, heartbeat)
    options = vars(Options(ocr=False, translate_figures=False, vision_review=False))
    request = {"kind": "queue", "mode": "analyze", "files": [str(source)], "output": str(directory),
               "options": options, "provider": {}, "autoexport": False}

    def wait_job(request, cancel=False, close=False):
        app._launch_process(request)
        if cancel:
            app._cancel()
        if close:
            root.after_cancel(heartbeat_id)
            app._close()
        deadline = time.monotonic() + 90
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.005)
        if app.busy:
            app._cancel()
            raise RuntimeError("Stress worker timed out")

    with patch("papertranslator.gui.messagebox.showerror", side_effect=lambda *a, **k: errors.append(str(a))), \
            patch("papertranslator.gui.messagebox.askyesno", return_value=True):
        wait_job(request)
        assert app.project and len(app.project.pages) == 4
        app.notebook.select(app.review_tab)
        root.update()
        blocks = list(app.project.blocks)
        for number in range(1500):
            block = blocks[number % len(blocks)]
            app.block_tree.selection_set(block.id)
            app._select_block()
            if number % 20 == 0:
                app.page = (number // 20) % len(app.project.pages)
            if number % 100 == 0:
                app.zoom_var.set(["60%", "100%", "150%", "200%"][number // 100 % 4])
            if number % 10 == 0:
                app.notebook.select([app.queue_tab, app.settings_tab][number // 10 % 2])
                root.update()
                app.notebook.select(app.review_tab)
            app._render_preview()
            app._log(f"Offline stress {number}: repeated selection, zoom, page and tab changes.")
            root.update()
            if number % 250 == 249:
                samples.append(resources(root) | {"iteration": number + 1})
        assert samples[-1]["tk_images"] <= samples[0]["tk_images"] + 1
        assert samples[-1]["gdi"] <= samples[0]["gdi"] + 10
        assert samples[-1]["handles"] <= samples[0]["handles"] + 10
        assert all(len(canvas.find_all()) <= 2 for canvas in app.canvases)
        # Exercise the actual App IPC path while Windows continues to paint.
        request["files"] = [str(directory / "forty-pages.pdf")]
        wait_job(request)
        project, path = app.project, app.project_file
        assert len(project.pages) == 40
        for block in project.blocks:
            if not block.protected:
                block.translation = "测试：" + block.text
                block.status = "translated"
        project.save(path)
        wait_job({"kind": "export", "project_path": str(path), "options": options,
                  "output": str(directory / "forty-pages.translated.pdf")})
        assert app.project.output and Path(app.project.output).is_file(), (
            errors, app.project.output, app.status.get(), app.log_text.get("1.0", "end")[-6000:])
        wait_job(request, cancel=True)
        assert path.is_file()
        # Closing during a task cooperatively cancels, waits and then destroys Tk.
        wait_job(request, close=True)
    report = {"ok": not errors, "iterations": 1500, "samples": samples, "callback_errors": errors,
              "heartbeat_count": len(ticks), "largest_heartbeat_gap_seconds": max(gaps),
              "forty_page_export": True, "cancel_and_close": True}
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
