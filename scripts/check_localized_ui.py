"""Inspect mapped first-page layouts using isolated settings and no API calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from PIL import ImageGrab
    from papertranslator import __version__
    from papertranslator.gui import App, make_root
    from papertranslator.localization import UI_LANGUAGES
    from papertranslator.settings import save_ui_language
    directory = ROOT / "test-output" / f"v{__version__}-languages"
    directory.mkdir(parents=True, exist_ok=True)
    results = []
    for code in UI_LANGUAGES:
        os.environ["PAPERTRANSLATOR_DATA_DIR"] = str(directory / "settings" / code)
        save_ui_language(code)
        root = make_root()
        app = App(root)
        root.geometry("1050x740+40+40")
        errors = []
        root.report_callback_exception = lambda *exc: errors.append(str(exc[1]))
        try:
            root.update()
            bounds = (root.winfo_rootx(), root.winfo_rootx() + root.winfo_width())
            visible = all(bounds[0] <= widget.winfo_rootx()
                and widget.winfo_rootx() + widget.winfo_width() <= bounds[1]
                and widget.winfo_width() >= widget.winfo_reqwidth()
                for widget in (app.ui_language_combo, app.ui_language_label, app.opensource_label))
            results.append({"code": code, "header_fits_minimum_window": visible, "callback_errors": errors})
            if code in ("zh-CN", "en", "de", "ar", "hi"):
                root.lift()
                root.update()
                time.sleep(0.15)
                # Pillow captures the HWND in physical pixels. Tk coordinates
                # may be DPI-virtualized and would crop the right/bottom edges.
                with ImageGrab.grab(window=root.winfo_id()) as screen:
                    screen.save(directory / f"home-{code}.png")
        finally:
            app._destroy()
    report = {"ok": all(r["header_fits_minimum_window"] and not r["callback_errors"] for r in results), "languages": results}
    (directory / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
