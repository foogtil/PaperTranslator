from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main():
    # PyInstaller's Windows child process must dispatch before argument parsing
    # or importing Tk, otherwise it would start another main window.
    import multiprocessing
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="文献译境 / PaperTranslator")
    parser.add_argument("files", nargs="*", help="打开 PDF 文献")
    parser.add_argument("--self-test", metavar="REPORT", help="离线检查打包组件，写入 JSON 报告")
    parser.add_argument("--project", help="打开已保存的翻译项目")
    args = parser.parse_args()
    if args.self_test:
        from .selftest import self_test
        result = self_test(args.self_test)
        return 0 if result["ok"] else 1
    from .gui import App, make_root
    root = make_root()
    app = App(root)
    if args.files:
        app._accept_paths(args.files)
    if args.project:
        from .models import Project
        project = Project.load(args.project)
        app._accept_paths([project.source])
        app._show_project(project, Path(args.project).resolve())
        app._load_options(project.options)
        app.notebook.select(app.review_tab)
        root.after(200, app._render_preview)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
