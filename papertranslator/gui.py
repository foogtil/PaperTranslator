from __future__ import annotations

import copy
import csv
import io
import json
import math
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
import traceback
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageDraw, ImageTk
import pymupdf as fitz

from . import __version__, PROJECT_URL
from .api import ChatClient, authentication_headers, validate_connection
from .engine import project_path, cached_project_path
from .models import Block, Cancelled, LANGUAGES, MODEL_PRESETS, Options, PRESETS, Project, Provider, dataclass_from
from .settings import data_dir, load_settings, save_settings
from .worker import PDFWorker


BG = "#f2f5fa"
INK = "#172742"
MUTED = "#63718a"
BLUE = "#285fe8"
LOG_LINES = 2000
LOG_CHARACTERS = 200_000
LOG_ENTRY_CHARACTERS = 4000
PREVIEW_PIXELS = 4_000_000
PREVIEW_EDGE = 4096


class Scrollable(ttk.Frame):
    """Keep every control reachable on small / scaled Windows displays."""
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.content = ttk.Frame(self.canvas)
        self.window_id = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        self.content.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window_id, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._wheel, add="+")

    def _wheel(self, event):
        widget = event.widget
        if widget.winfo_class() in ("Text", "Treeview", "TCombobox", "Listbox"):
            return
        while widget is not None:
            if widget is self.content or widget is self.canvas:
                self.canvas.yview_scroll(-int(event.delta / 120), "units")
                return
            widget = getattr(widget, "master", None)


def make_root():
    try:
        from tkinterdnd2 import TkinterDnD
        return TkinterDnD.Tk()
    except (ImportError, tk.TclError):
        return tk.Tk()


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("文献译境 · PaperTranslator")
        screen_width, screen_height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width, height = min(1440, screen_width - 70), min(940, screen_height - 100)
        self.root.geometry(f"{width}x{height}+{max(0, (screen_width - width) // 2)}+20")
        self.root.minsize(min(1050, width), min(680, height))
        self.root.configure(bg=BG)
        icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "assets" / "icon.ico"
        if icon.is_file():
            self.root.iconbitmap(str(icon))
        self.settings = load_settings()
        self.profiles = self.settings.get("profiles", {})
        self.current_provider = self.settings.get("provider", "DeepSeek")
        if self.current_provider not in PRESETS:
            self.current_provider = "DeepSeek"
        self.files = []
        self.projects = {}
        self.project = None
        self.project_file = None
        self.busy = False
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.worker = None
        self.pdf_job = None
        self.export_progress = None
        self.export_changed_at = 0.0
        self.export_status_second = -1
        self.export_warned = False
        self.closing = False
        self.page = 0
        self.preview_scale = 1.0
        self.tk_images = [None, None]
        self.preview_keys = [None, None]
        self.preview_scales = [None, None]
        self.preview_after = None
        self.preview_rendering = False
        self.selection_start = None
        self.manual_mode = False
        self._style()
        self._build()
        self._load_profile(self.current_provider)
        self._load_options()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.poll_after = self.root.after(100, self._poll)

    def _style(self):
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background=BG, foreground=INK, font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("TButton", padding=(13, 7), font=("Microsoft YaHei UI", 10))
        style.configure("Primary.TButton", background=BLUE, foreground="white", borderwidth=0)
        style.map("Primary.TButton", background=[("active", "#1b48ba"), ("disabled", "#a6b9e7")])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(24, 11), background="#e8edf6", foreground=MUTED)
        style.map("TNotebook.Tab", background=[("selected", "white")], foreground=[("selected", BLUE)])
        style.configure("Treeview", rowheight=32, font=("Microsoft YaHei UI", 10), background="white", fieldbackground="white", borderwidth=0)
        style.configure("Treeview.Heading", background="#e9eef7", foreground=INK, padding=7)
        style.map("Treeview", background=[("selected", "#dce8ff")], foreground=[("selected", INK)])
        style.configure("TLabelframe", background=BG, bordercolor="#d8dfeb")
        style.configure("TLabelframe.Label", background=BG, foreground=INK, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TCheckbutton", background=BG)
        self._checkbutton_style(style)
        style.configure("Horizontal.TProgressbar", background=BLUE, troughcolor="#e3e9f4", borderwidth=0)

    def _checkbutton_style(self, style):
        # Replace clam's cross indicator everywhere, retaining native checkbutton
        # variables, mouse / keyboard bindings, focus and disabled behaviour.
        size = max(16, round(18 * self.root.winfo_fpixels("1i") / 96))
        gap, supersample = max(5, round(size / 3)), 4
        images = {}
        for selected, disabled in ((False, False), (True, False), (False, True), (True, True)):
            canvas = Image.new("RGBA", ((size + gap) * supersample, (size + 2) * supersample))
            draw = ImageDraw.Draw(canvas)
            fill = ("#a3aec0" if disabled else BLUE) if selected else ("#eef1f6" if disabled else "white")
            border = "#b5bfcd" if disabled else (BLUE if selected else "#8b98ab")
            draw.rounded_rectangle((supersample, supersample, (size - 1) * supersample, (size - 1) * supersample),
                                   radius=2 * supersample, fill=fill, outline=border, width=supersample)
            if selected:
                draw.line([(round(x * size * supersample), round(y * size * supersample))
                           for x, y in ((0.23, 0.51), (0.43, 0.71), (0.78, 0.29))],
                          fill="white", width=max(2, round(size * 0.12)) * supersample, joint="curve")
            canvas = canvas.resize((size + gap, size + 2), Image.Resampling.LANCZOS)
            images[selected, disabled] = ImageTk.PhotoImage(canvas, master=self.root)
        # Keep these references independent of the two PDF preview images.
        self.check_images = images
        element = "PaperTranslator.Check.indicator"
        style.element_create(element, "image", images[False, False],
                             ("disabled", "selected", images[True, True]),
                             ("disabled", images[False, True]),
                             ("selected", images[True, False]), sticky="")
        def replace(layout):
            return [(element if name == "Checkbutton.indicator" else name,
                     {key: replace(value) if key == "children" else value for key, value in options.items()})
                    for name, options in layout]
        style.layout("TCheckbutton", replace(style.layout("TCheckbutton")))

    def _button(self, parent, text, command, primary=False, **kwargs):
        return ttk.Button(parent, text=text, command=command, style="Primary.TButton" if primary else "TButton", **kwargs)

    def _build(self):
        header = tk.Frame(self.root, bg=INK, height=96)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="文献译境", bg=INK, fg="white", font=("Microsoft YaHei UI", 23, "bold")).pack(side="left", padx=(26, 16))
        tk.Label(header, text="PDF 原位翻译与校对", bg=INK, fg="#b9c8e2", justify="left", font=("Microsoft YaHei UI", 10)).pack(side="left")
        release = tk.Frame(header, bg=INK)
        release.pack(side="right", padx=24)
        tk.Label(release, text=f"PaperTranslator  {__version__}  ·  Windows x64", bg=INK, fg="#b9c8e2").pack(anchor="e")
        self.opensource_label = tk.Label(release, text="PaperTranslator 已在 GitHub 上开源", bg=INK, fg="#d5e3ff",
                                         cursor="hand2" if PROJECT_URL else "")
        self.opensource_label.pack(anchor="e", pady=(6, 0))
        if PROJECT_URL:
            self.opensource_label.bind("<Button-1>", lambda event: webbrowser.open(PROJECT_URL))
        body = ttk.Frame(self.root, padding=(22, 16))
        body.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill="both", expand=True)
        self.queue_tab = ttk.Frame(self.notebook, padding=18)
        self.review_tab = ttk.Frame(self.notebook, padding=12)
        self.settings_tab = ttk.Frame(self.notebook, padding=18)
        self.notebook.add(self.queue_tab, text="01  翻译队列")
        self.notebook.add(self.review_tab, text="02  校对与预览")
        self.notebook.add(self.settings_tab, text="03  接口与规则")
        self.queue_actions = ttk.Frame(self.queue_tab)
        self.queue_actions.pack(side="bottom", fill="x")
        queue_scroll = Scrollable(self.queue_tab)
        queue_scroll.pack(fill="both", expand=True)
        self.queue_content = queue_scroll.content
        settings_scroll = Scrollable(self.settings_tab)
        settings_scroll.pack(fill="both", expand=True)
        self.settings_content = settings_scroll.content
        self._queue_ui()
        self._review_ui()
        self._settings_ui()
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self._schedule_preview())
        footer = ttk.Frame(body, padding=(0, 10, 0, 0))
        footer.pack(fill="x")
        self.status = tk.StringVar(value="就绪 · 添加 PDF 后，先分析版面，再翻译与校对。")
        ttk.Label(footer, textvariable=self.status, style="Muted.TLabel").pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(footer, length=200, mode="indeterminate")
        self.progress.pack(side="right")

    def _queue_ui(self):
        top = ttk.Frame(self.queue_content)
        top.pack(fill="x", pady=(0, 12))
        ttk.Label(top, text="把论文带进来，让译文回到原来的位置。", style="Title.TLabel").pack(side="left")
        self._button(top, "打开已保存项目", self._open_project).pack(side="right")
        middle = ttk.Frame(self.queue_content)
        middle.pack(fill="both", expand=True)
        left = ttk.Frame(middle)
        left.pack(side="left", fill="both", expand=True, padx=(0, 18))
        actions = ttk.Frame(left)
        actions.pack(fill="x", pady=(0, 9))
        self._button(actions, "＋ 添加 PDF", self._add_files, True).pack(side="left")
        self._button(actions, "移除所选", self._remove_files).pack(side="left", padx=7)
        self._button(actions, "载入校对", self._load_selected).pack(side="left")
        self.file_tree = ttk.Treeview(left, columns=("name", "state"), show="headings", selectmode="extended", height=8)
        self.file_tree.heading("name", text="PDF 文献 / 支持直接拖入多个文件")
        self.file_tree.heading("state", text="状态")
        self.file_tree.column("name", width=400)
        self.file_tree.column("state", width=150, stretch=False)
        self.file_tree.pack(fill="both", expand=True)
        self.file_tree.bind("<Double-1>", lambda e: self._load_selected())
        try:
            from tkinterdnd2 import DND_FILES
            self.file_tree.drop_target_register(DND_FILES)
            self.file_tree.dnd_bind("<<Drop>>", self._drop)
        except (ImportError, AttributeError, tk.TclError):
            self.file_tree.heading("name", text="PDF 文献 / 使用“添加 PDF”导入")
        ttk.Label(left, text="保持原页数、页面尺寸和方向；公式与数值保留。复杂区域会标出供校对。", style="Muted.TLabel", wraplength=650).pack(anchor="w", pady=9)
        panel = ttk.LabelFrame(middle, text="本次翻译", padding=14)
        panel.pack(side="right", fill="y")
        self.source_var = tk.StringVar(value="英语")
        self.target_var = tk.StringVar(value="简体中文")
        ttk.Label(panel, text="原文语言").grid(row=0, column=0, sticky="w")
        ttk.Combobox(panel, textvariable=self.source_var, values=LANGUAGES, width=13).grid(row=1, column=0, sticky="ew", pady=(5, 12))
        ttk.Label(panel, text="目标语言").grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Combobox(panel, textvariable=self.target_var, values=LANGUAGES[1:], width=13).grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=(5, 12))
        ttk.Label(panel, text="处理页码（留空为全部）").grid(row=2, column=0, columnspan=2, sticky="w")
        self.pages_var = tk.StringVar()
        ttk.Entry(panel, textvariable=self.pages_var).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(5, 10))
        ttk.Label(panel, text="例如 1-5,8,10-12；未选页面保持原样", style="Muted.TLabel").grid(row=4, column=0, columnspan=2, sticky="w")
        self.ocr_var = tk.BooleanVar(value=True)
        self.figures_var = tk.BooleanVar(value=True)
        self.rotations_var = tk.BooleanVar(value=True)
        self.vision_var = tk.BooleanVar(value=True)
        self.autoexport_var = tk.BooleanVar(value=True)
        self.review_marks_var = tk.BooleanVar(value=False)
        self.export_json_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(panel, text="识别扫描页面正文", variable=self.ocr_var).grid(row=5, column=0, columnspan=2, sticky="w", pady=(15, 4))
        ttk.Checkbutton(panel, text="识别图内文字，按整图翻译", variable=self.figures_var).grid(row=6, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(panel, text="四方向识别（质量优先）", variable=self.rotations_var).grid(row=7, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(panel, text="视觉核对（整图及漏识别文字）", variable=self.vision_var).grid(row=8, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(panel, text="翻译后自动导出 PDF", variable=self.autoexport_var).grid(row=9, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(panel, text="PDF 显示待校对批注（黄色图标）", variable=self.review_marks_var).grid(row=10, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(panel, text="同时导出 JSON（校对项目和报告）", variable=self.export_json_var).grid(row=11, column=0, columnspan=2, sticky="w", pady=4)
        self.provider_badge = tk.StringVar()
        ttk.Label(panel, textvariable=self.provider_badge, foreground=BLUE, wraplength=310).grid(row=12, column=0, columnspan=2, sticky="w", pady=14)
        self._button(panel, "配置接口与术语 →", lambda: self.notebook.select(self.settings_tab)).grid(row=13, column=0, columnspan=2, sticky="ew")
        output = ttk.Frame(self.queue_actions)
        output.pack(fill="x", pady=12)
        ttk.Label(output, text="输出目录").pack(side="left", padx=(0, 10))
        self.output_var = tk.StringVar(value=self.settings.get("output_dir", ""))
        ttk.Entry(output, textvariable=self.output_var).pack(side="left", fill="x", expand=True)
        self._button(output, "选择", self._choose_output).pack(side="left", padx=(8, 0))
        controls = ttk.Frame(self.queue_actions)
        controls.pack(fill="x", pady=(0, 9))
        self.analyze_button = self._button(controls, "① 分析版面 / 估算费用", lambda: self._run("analyze"))
        self.analyze_button.pack(side="left")
        self.translate_button = self._button(controls, "② 开始 / 继续翻译", lambda: self._run("translate"), True)
        self.translate_button.pack(side="left", padx=8)
        self.cancel_button = self._button(controls, "取消任务", self._cancel, state="disabled")
        self.cancel_button.pack(side="left")
        self.force_export_button = self._button(controls, "强制停止排版", self._force_export_stop, state="disabled")
        self.force_export_button.pack(side="left", padx=8)
        self._button(controls, "打开输出目录", self._open_output).pack(side="right")
        self.log_text = tk.Text(self.queue_content, height=5, bg="#e9eef6", fg=INK, relief="flat", padx=10, pady=8, wrap="word", state="disabled", font=("Microsoft YaHei UI", 9))
        self.log_text.pack(fill="x")

    def _review_ui(self):
        bar = ttk.Frame(self.review_tab)
        bar.pack(fill="x", pady=(0, 8))
        self.review_title = tk.StringVar(value="先分析 PDF 或打开已保存项目")
        ttk.Label(bar, textvariable=self.review_title, font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        self._button(bar, "导出校对后的 PDF", self._export_current, True).pack(side="right")
        export_controls = ttk.Frame(self.review_tab)
        export_controls.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(export_controls, text="PDF 显示待校对批注（黄色图标）", variable=self.review_marks_var).pack(side="left")
        ttk.Checkbutton(export_controls, text="同时导出 JSON（校对项目和报告）", variable=self.export_json_var).pack(side="left", padx=(22, 0))
        panes = ttk.Panedwindow(self.review_tab, orient="horizontal")
        panes.pack(fill="both", expand=True)
        editor_shell = Scrollable(panes, width=390)
        editor = editor_shell.content
        viewer = ttk.Frame(panes)
        panes.add(editor_shell, weight=3)
        panes.add(viewer, weight=7)
        filterbar = ttk.Frame(editor)
        filterbar.pack(fill="x", pady=(0, 7))
        ttk.Label(filterbar, text="段落与整图（展开图片校对标签）").pack(side="left")
        self.filter_var = tk.StringVar(value="全部")
        filt = ttk.Combobox(filterbar, values=["全部", "待校对", "可翻译", "已翻译"], textvariable=self.filter_var, width=10, state="readonly")
        filt.pack(side="right")
        filt.bind("<<ComboboxSelected>>", lambda e: self._fill_blocks())
        self.block_tree = ttk.Treeview(editor, columns=("page", "text", "state"), show="tree headings", height=7)
        self.block_tree.column("#0", width=22, stretch=False)
        self.block_tree.heading("#0", text="")
        for column, title, width in [("page", "页", 34), ("text", "原文区域", 210), ("state", "状态", 84)]:
            self.block_tree.heading(column, text=title)
            self.block_tree.column(column, width=width, stretch=column == "text")
        self.block_tree.pack(fill="both", expand=True)
        self.block_tree.bind("<<TreeviewSelect>>", self._select_block)
        ttk.Label(editor, text="原文 / 可修正 OCR").pack(anchor="w", pady=(8, 3))
        self.source_text = tk.Text(editor, height=4, wrap="word", relief="solid", bd=1, font=("Microsoft YaHei UI", 10))
        self.source_text.pack(fill="x")
        ttk.Label(editor, text="译文 / 修改后需重新导出").pack(anchor="w", pady=(7, 3))
        self.translation_text = tk.Text(editor, height=5, wrap="word", relief="solid", bd=1, font=("Microsoft YaHei UI", 10))
        self.translation_text.pack(fill="x")
        self.keep_var = tk.BooleanVar()
        self.approved_var = tk.BooleanVar()
        ttk.Checkbutton(editor, text="保留此区域原文（公式 / 代码等）", variable=self.keep_var).pack(anchor="w", pady=(6, 0))
        ttk.Checkbutton(editor, text="已人工核对原文及图像背景", variable=self.approved_var).pack(anchor="w")
        geom = ttk.Frame(editor)
        geom.pack(fill="x", pady=4)
        ttk.Label(geom, text="方向°").pack(side="left")
        self.angle_var = tk.StringVar(value="0")
        ttk.Entry(geom, textvariable=self.angle_var, width=7).pack(side="left", padx=4)
        self.vertical_var = tk.BooleanVar()
        ttk.Checkbutton(geom, text="字符竖排", variable=self.vertical_var).pack(side="left")
        self._button(editor, "保存区域修改", self._save_block, True).pack(fill="x", pady=5)
        self.note_var = tk.StringVar(value="点选区域可查看原文、译文和校对提示。")
        ttk.Label(editor, textvariable=self.note_var, style="Muted.TLabel", wraplength=350).pack(fill="x", pady=4)
        navigation = ttk.Frame(viewer)
        navigation.pack(fill="x", padx=(12, 0), pady=(0, 6))
        self._button(navigation, "‹", lambda: self._change_page(-1), width=3).pack(side="left")
        self.page_label = tk.StringVar(value="0 / 0")
        ttk.Label(navigation, textvariable=self.page_label).pack(side="left", padx=8)
        self._button(navigation, "›", lambda: self._change_page(1), width=3).pack(side="left")
        self.zoom_var = tk.StringVar(value="100%")
        zoom = ttk.Combobox(navigation, textvariable=self.zoom_var, values=["60%", "80%", "100%", "125%", "150%", "200%"], width=7, state="readonly")
        zoom.pack(side="left", padx=8)
        zoom.bind("<<ComboboxSelected>>", lambda e: self._schedule_preview())
        self._button(navigation, "框选补充区域", self._manual_toggle).pack(side="right")
        pages = ttk.Frame(viewer)
        pages.pack(fill="both", expand=True, padx=(12, 0))
        self.canvases = []
        for title in ("原文", "译文 PDF（导出后更新）"):
            frame = ttk.Frame(pages)
            frame.pack(side="left", fill="both", expand=True, padx=3)
            ttk.Label(frame, text=title, foreground=MUTED).pack(anchor="w", pady=4)
            canvas = tk.Canvas(frame, bg="#dce3ed", highlightthickness=0, width=360)
            scroll = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
            scroll.pack(side="right", fill="y")
            canvas.configure(yscrollcommand=scroll.set)
            horizontal = ttk.Scrollbar(frame, orient="horizontal", command=canvas.xview)
            horizontal.pack(side="bottom", fill="x")
            canvas.configure(xscrollcommand=horizontal.set)
            canvas.pack(fill="both", expand=True)
            canvas.bind("<MouseWheel>", self._wheel)
            canvas.bind("<Configure>", lambda e: self._schedule_preview(120))
            self.canvases.append(canvas)
        self.canvases[0].bind("<ButtonPress-1>", self._manual_start)
        self.canvases[0].bind("<B1-Motion>", self._manual_drag)
        self.canvases[0].bind("<ButtonRelease-1>", self._manual_finish)

    def _settings_ui(self):
        left = ttk.LabelFrame(self.settings_content, text="API 接口 · 每位使用者填写自己的密钥", padding=16)
        left.pack(side="left", fill="both", expand=True, padx=(0, 18))
        right = ttk.LabelFrame(self.settings_content, text="排版与术语", padding=16)
        right.pack(side="right", fill="both", expand=True)
        self.provider_var = tk.StringVar(value=self.current_provider)
        ttk.Label(left, text="服务商预设").grid(row=0, column=0, sticky="w", pady=7)
        providerbox = ttk.Combobox(left, textvariable=self.provider_var, values=list(PRESETS), state="readonly")
        providerbox.grid(row=0, column=1, sticky="ew", pady=7)
        providerbox.bind("<<ComboboxSelected>>", self._switch_profile)
        self.base_var, self.model_var, self.key_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for row, label, var in [(1, "Base URL", self.base_var), (2, "模型 ID", self.model_var), (3, "API Key", self.key_var)]:
            ttk.Label(left, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=9)
            if row == 2:
                self.model_combo = ttk.Combobox(left, textvariable=var, values=MODEL_PRESETS[self.current_provider], width=38)
                self.model_combo.grid(row=row, column=1, sticky="ew", pady=9)
            else:
                ttk.Entry(left, textvariable=var, show="●" if row == 3 else "", width=38).grid(row=row, column=1, sticky="ew", pady=9)
        self.auth_var = tk.StringVar(value="Authorization")
        ttk.Label(left, text="鉴权请求头").grid(row=4, column=0, sticky="w", pady=8)
        ttk.Combobox(left, textvariable=self.auth_var, values=["Authorization", "api-key", "x-api-key"], state="readonly").grid(row=4, column=1, sticky="ew", pady=8)
        self.vision_cap_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text="此模型支持图片输入", variable=self.vision_cap_var).grid(row=5, column=0, columnspan=2, sticky="w", pady=7)
        self.remember_var = tk.BooleanVar(value=self.settings.get("remember", False))
        ttk.Checkbutton(left, text="在此 Windows 用户下加密记住密钥", variable=self.remember_var).grid(row=6, column=0, columnspan=2, sticky="w", pady=7)
        ttk.Label(left, text="密钥不写入 PDF、校对项目或发布包。\n本地识别无需联网；翻译发送文字，视觉核对发送局部截图。", wraplength=460, style="Muted.TLabel").grid(row=7, column=0, columnspan=2, sticky="w", pady=10)
        self._button(left, "测试连接", self._test_api).grid(row=8, column=0, sticky="w", pady=8)
        self._button(left, "保存设置", self._save_all, True).grid(row=8, column=1, sticky="e", pady=8)
        self.input_price_var = tk.StringVar(value="0")
        self.output_price_var = tk.StringVar(value="0")
        self.currency_var = tk.StringVar(value="CNY")
        for row, label, var in [(9, "输入 / 百万 Token", self.input_price_var), (10, "输出 / 百万 Token", self.output_price_var), (11, "费用币种", self.currency_var)]:
            ttk.Label(left, text=label).grid(row=row, column=0, sticky="w", pady=6)
            ttk.Entry(left, textvariable=var, width=18).grid(row=row, column=1, sticky="w", pady=6)
        ttk.Label(left, text="单价由你按服务商账单填写；0 表示未设置。\n估算不含视觉输入、重试及推理 Token。", style="Muted.TLabel").grid(row=12, column=0, columnspan=2, sticky="w", pady=10)
        left.columnconfigure(1, weight=1)
        self.dpi_var = tk.StringVar(value="220")
        self.minfont_var = tk.StringVar(value="6")
        self.font_var = tk.StringVar()
        ttk.Label(right, text="OCR 分辨率 DPI").pack(anchor="w")
        ttk.Combobox(right, textvariable=self.dpi_var, values=["150", "180", "220", "300", "400"], width=16).pack(anchor="w", pady=(5, 10))
        ttk.Label(right, text="译文最小字号 pt（原文字号更小则沿用原下限）").pack(anchor="w")
        ttk.Entry(right, textvariable=self.minfont_var, width=18).pack(anchor="w", pady=(5, 10))
        ttk.Label(right, text="自定义字体（可选 .ttf / .otf / .ttc）").pack(anchor="w")
        fontrow = ttk.Frame(right)
        fontrow.pack(fill="x", pady=(5, 10))
        ttk.Entry(fontrow, textvariable=self.font_var).pack(side="left", fill="x", expand=True)
        self._button(fontrow, "选择", self._choose_font).pack(side="left", padx=(6, 0))
        ttk.Label(right, text="术语表：每行 原词=译词", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(7, 5))
        self.glossary_text = tk.Text(right, height=10, relief="solid", bd=1, wrap="word", font=("Microsoft YaHei UI", 10))
        self.glossary_text.pack(fill="both", expand=True)
        self._button(right, "导入 CSV / TSV 术语表", self._import_glossary).pack(anchor="w", pady=8)
        ttk.Label(right, text="默认翻译正文、图表文字、图题、脚注及参考文献标题。\n公式、变量、数值、代码和引用标识保留；译文溢出时保留原文并标注。", style="Muted.TLabel", wraplength=470).pack(anchor="w", pady=8)
        self._button(right, "关于 / 开源许可 / 使用说明", self._about).pack(anchor="w")

    def _capture_profile(self):
        price_in, price_out = float(self.input_price_var.get() or "0"), float(self.output_price_var.get() or "0")
        if not all(math.isfinite(p) and p >= 0 for p in (price_in, price_out)):
            raise ValueError("Token 单价必须是非负有限数值。")
        return Provider(name=self.provider_var.get(), base_url=self.base_var.get().strip(), model=self.model_var.get().strip(),
                        api_key=self.key_var.get().strip(" \t\r\n"), auth_header=self.auth_var.get(), vision=self.vision_cap_var.get(),
                        input_price=price_in, output_price=price_out, currency=self.currency_var.get().strip() or "CNY")

    def _load_profile(self, name):
        saved = self.profiles.get(name, vars(PRESETS[name]))
        profile = dataclass_from(Provider, saved)
        self.model_combo.configure(values=MODEL_PRESETS[name] or ((profile.model,) if profile.model else ()))
        for var, value in [(self.base_var, profile.base_url), (self.model_var, profile.model), (self.key_var, profile.api_key),
                           (self.auth_var, profile.auth_header), (self.input_price_var, profile.input_price),
                           (self.output_price_var, profile.output_price), (self.currency_var, profile.currency)]:
            var.set(value)
        self.vision_cap_var.set(profile.vision)
        self.provider_badge.set(f"{name}  /  {profile.model}\n在“接口与规则”中可修改模型及地址")

    def _switch_profile(self, event=None):
        try:
            previous = self._capture_profile()
            previous.name = self.current_provider
            self.profiles[self.current_provider] = vars(previous).copy()
        except ValueError:
            pass
        self.current_provider = self.provider_var.get()
        self._load_profile(self.current_provider)

    def _options(self):
        glossary = {}
        for i, line in enumerate(self.glossary_text.get("1.0", "end").splitlines(), 1):
            if not line.strip() or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(f"术语表第 {i} 行缺少 = 分隔符。")
            key, value = line.split("=", 1)
            if not key.strip() or not value.strip():
                raise ValueError(f"术语表第 {i} 行不能为空。")
            glossary[key.strip()] = value.strip()
        result = Options(source_language=self.source_var.get().strip(), target_language=self.target_var.get().strip(),
                         page_range=self.pages_var.get().strip(), ocr=self.ocr_var.get(), ocr_dpi=int(self.dpi_var.get()),
                         ocr_rotations=self.rotations_var.get(), vision_review=self.vision_var.get(), minimum_font=float(self.minfont_var.get()),
                         font_path=self.font_var.get().strip(), glossary=glossary, translate_figures=self.figures_var.get(),
                         pdf_review_marks=self.review_marks_var.get(), export_json=self.export_json_var.get())
        result.validate()
        return result

    def _load_options(self, options=None):
        o = dataclass_from(Options, options if options is not None else self.settings.get("options", {}))
        if options is None:
            self.review_marks_var.set(o.pdf_review_marks)
            self.export_json_var.set(o.export_json)
        for var, value in [(self.source_var, o.source_language), (self.target_var, o.target_language), (self.pages_var, o.page_range),
                           (self.ocr_var, o.ocr), (self.figures_var, o.translate_figures), (self.rotations_var, o.ocr_rotations), (self.vision_var, o.vision_review),
                           (self.dpi_var, o.ocr_dpi), (self.minfont_var, o.minimum_font), (self.font_var, o.font_path)]:
            var.set(value)
        self.glossary_text.delete("1.0", "end")
        self.glossary_text.insert("1.0", "\n".join(f"{k}={v}" for k, v in o.glossary.items()))

    def _save_all(self, show=True, validate=True):
        try:
            provider = self._capture_profile()
            if validate:
                authentication_headers(provider, require_key=False)
            options = self._options()
            self.profiles[self.provider_var.get()] = vars(provider).copy()
            settings = {"profiles": self.profiles, "provider": self.provider_var.get(), "options": vars(options),
                        "remember": self.remember_var.get(), "output_dir": self.output_var.get()}
            save_settings(settings, self.remember_var.get())
            self.provider_badge.set(f"{provider.name}  /  {provider.model}\n文字翻译 + {'视觉核对' if provider.vision else '本地 OCR'}")
            if show:
                self._log("设置已保存。" + ("密钥已按当前 Windows 用户加密。" if self.remember_var.get() else "密钥仅在本次会话中使用。"))
            return True
        except (ValueError, OSError, RuntimeError) as e:
            messagebox.showerror("设置未保存", str(e), parent=self.root)
            return False

    def _add_files(self):
        if self.busy:
            return
        paths = filedialog.askopenfilenames(title="添加 PDF 文献", filetypes=[("PDF 文档", "*.pdf")])
        self._accept_paths(paths)

    def _drop(self, event):
        if not self.busy:
            self._accept_paths(self.root.tk.splitlist(event.data))

    def _accept_paths(self, paths):
        for item in paths:
            path = str(Path(item).resolve())
            if Path(path).suffix.lower() != ".pdf" or not Path(path).is_file() or path in self.files:
                continue
            self.files.append(path)
            self.file_tree.insert("", "end", iid=str(len(self.files) - 1), values=(Path(path).name, "等待分析"))
        if self.files and not self.output_var.get():
            self.output_var.set(str(Path(self.files[0]).parent / "译文"))

    def _remove_files(self):
        if self.busy:
            return
        selected = {int(i) for i in self.file_tree.selection()}
        for i in selected:
            self.projects.pop(self.files[i], None)
        self.files = [p for i, p in enumerate(self.files) if i not in selected]
        self.file_tree.delete(*self.file_tree.get_children())
        for i, path in enumerate(self.files):
            self.file_tree.insert("", "end", iid=str(i), values=(Path(path).name, "已分析" if path in self.projects else "等待分析"))

    def _choose_output(self):
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.output_var.set(path)

    def _choose_font(self):
        path = filedialog.askopenfilename(title="选择字体", filetypes=[("字体", "*.ttf *.otf *.ttc")])
        if path:
            self.font_var.set(path)

    def _open_output(self):
        path = Path(self.output_var.get())
        if path.is_dir():
            os.startfile(str(path))

    def _set_busy(self, value):
        self.busy = value
        self.analyze_button.configure(state="disabled" if value else "normal")
        self.translate_button.configure(state="disabled" if value else "normal")
        self.cancel_button.configure(state="normal" if value else "disabled")
        self.force_export_button.configure(state="disabled")
        self.export_progress = None
        self.progress.stop()
        self.progress.configure(mode="indeterminate", maximum=100, value=0)
        if value:
            self.progress.start(12)

    def _launch(self, task):
        if self.busy:
            return
        self.cancel = threading.Event()
        self._set_busy(True)

        def work():
            try:
                task()
            except Cancelled as error:
                self.events.put(("log", str(error)))
            except Exception as error:
                safe = str(error)
                for profile in self.profiles.values():
                    secret = profile.get("api_key", "")
                    if secret:
                        safe = safe.replace(secret, "[已隐藏密钥]")
                self.events.put(("error", safe))
            finally:
                self.events.put(("idle", None))

        self.worker = threading.Thread(target=work, name="translation-worker", daemon=True)
        self.worker.start()

    def _launch_process(self, request):
        if self.busy:
            return
        job = PDFWorker(request)
        self.pdf_job = job
        self.cancel = job.cancel
        self._set_busy(True)
        try:
            job.start()
        except Exception as error:
            job.close()
            self.pdf_job = None
            self._set_busy(False)
            messagebox.showerror("后台任务未启动", str(error), parent=self.root)
            return
        self.worker = threading.Thread(target=job.relay, args=(self.events,),
                                       name="pdf-event-reader", daemon=True)
        self.worker.start()

    def _run(self, mode):
        if self.busy:
            return
        if not self.files:
            messagebox.showinfo("添加文献", "请先添加 PDF 文献。", parent=self.root)
            return
        try:
            options, provider = self._options(), self._capture_profile()
            if mode == "translate":
                try:
                    validate_connection(provider)
                except ValueError:
                    self.notebook.select(self.settings_tab)
                    raise
            output = Path(self.output_var.get()).resolve()
            if not self.output_var.get().strip():
                raise ValueError("请选择输出目录。")
            output.mkdir(parents=True, exist_ok=True)
            if not self._save_all(show=False, validate=mode == "translate"):
                return
        except (ValueError, OSError) as error:
            messagebox.showerror("无法开始", str(error), parent=self.root)
            return
        selected = self.file_tree.selection()
        files = [self.files[int(i)] for i in selected] if selected else list(self.files)
        autoexport = self.autoexport_var.get()
        for source in files:
            self._file_state(source, "准备处理")

        self._launch_process({"kind": "queue", "mode": mode, "files": files, "output": str(output),
                              "options": vars(options), "provider": vars(provider), "autoexport": autoexport})

    def _file_state(self, source, state):
        if source in self.files:
            iid = str(self.files.index(source))
            self.file_tree.item(iid, values=(Path(source).name, state))

    def _test_api(self):
        if self.busy:
            return
        try:
            provider = self._capture_profile()
            validate_connection(provider)
        except ValueError as error:
            messagebox.showerror("接口设置", str(error), parent=self.root)
            return

        def task():
            client = ChatClient(provider, self.cancel, on_log=lambda t: self.events.put(("log", t)))
            try:
                result = client.test()
                self.events.put(("info", f"接口连接成功，模型返回：{result}"))
            finally:
                client.close()
        self._launch(task)

    def _cancel(self):
        self.cancel.set()
        if self.export_progress:
            self.status.set("正在取消排版；等待当前步骤返回。若一直无响应，可使用“强制停止排版”。")
        else:
            self.status.set("正在取消；当前网络读取最多等待约 30 秒，完成部分会保存。")

    def _force_export_stop(self):
        job = self.pdf_job
        if not self.busy or job is None or not job.exporting:
            return
        if not messagebox.askyesno("停止当前排版", "将放弃本次尚未完成的 PDF 排版。\n已保存的译文、原 PDF 和之前成功导出的 PDF 会保留。\n\n是否停止？", parent=self.root):
            return
        if job.force_stop_export():
            self.export_progress = None
            self.cancel_button.configure(state="disabled")
            self.force_export_button.configure(state="disabled")
            self._log("正在停止排版工作进程；已有译文保留，稍后可重新导出。")

    def _export_update(self, value):
        if value.get("phase") == "complete":
            self.export_progress = None
            self.force_export_button.configure(state="disabled")
            self.progress.stop()
            self.progress.configure(mode="indeterminate", maximum=100, value=0)
            if self.busy:
                self.progress.start(12)
            self.status.set("PDF 导出完成；正在保存任务状态。")
            return
        self.export_progress = value
        self.export_changed_at = time.monotonic()
        self.export_status_second = -1
        self.export_warned = False
        total = value.get("total", 0)
        mode = "determinate" if total > 0 else "indeterminate"
        if str(self.progress.cget("mode")) != mode:
            self.progress.stop()
            self.progress.configure(mode=mode)
            if mode == "indeterminate":
                self.progress.start(12)
        if total > 0:
            self.progress.configure(maximum=total, value=min(value.get("current", 0), total))
        self.force_export_button.configure(state="normal" if self.pdf_job is not None and self.pdf_job.exporting else "disabled")
        self._export_status()

    def _export_status(self):
        if not self.busy or not self.export_progress:
            return
        elapsed = int(time.monotonic() - self.export_changed_at)
        if elapsed == self.export_status_second:
            return
        self.export_status_second = elapsed
        data = self.export_progress
        label = data["label"]
        if data.get("page"):
            label = f"第 {data['page']} 页 · " + label
        if data.get("total", 0) > 0:
            label += f" {data.get('current', 0)}/{data['total']}"
        text = f"排版 · {label} · 此步 {elapsed} 秒"
        if elapsed >= 60 and data.get("phase") != "complete":
            if not self.export_warned:
                self.export_warned = True
                self._log(f"{label} 已持续 {elapsed} 秒没有新进展；不等同于确定卡死，可继续等待或停止排版。")
            text += " · 暂无新进展，可等待或停止排版"
        self.status.set(text)

    def _poll(self):
        self.poll_after = None
        # Leave time for Windows paint / input messages even during log bursts.
        deadline = time.monotonic() + 0.02
        processed = 0
        try:
            while processed < 100 and time.monotonic() < deadline:
                kind, value = self.events.get_nowait()
                processed += 1
                if kind == "log":
                    self._log(value)
                elif kind == "error":
                    self._log("错误：" + value)
                    if not self.closing:
                        messagebox.showerror("操作未完成", value, parent=self.root)
                elif kind == "info":
                    self._log(value)
                    if not self.closing:
                        messagebox.showinfo("连接测试", value, parent=self.root)
                elif kind == "file_state":
                    self._file_state(*value)
                elif kind == "progress":
                    self._export_update(value)
                elif kind == "project_path":
                    source, path = value
                    try:
                        # The worker already verified the source hash. Its JSON
                        # snapshot is atomic and contains no live PDF objects.
                        project = Project.load(path, verify=False)
                        self.projects[source] = (project, Path(path))
                        self.project, self.project_file = project, Path(path)
                    except (ValueError, OSError, TypeError) as error:
                        self._log("无法载入任务结果：" + str(error))
                elif kind == "idle":
                    self.worker = None
                    self.pdf_job = None
                    self._set_busy(False)
                    self.status.set("任务已停止。可在“校对与预览”检查结果，或继续处理。")
                    if self.project and not self.closing:
                        self._show_project(self.project, self.project_file)
                    if self.closing:
                        self._destroy()
                        return
        except queue.Empty:
            pass
        self._export_status()
        self.poll_after = self.root.after(10 if not self.events.empty() else 100, self._poll)

    def _log(self, text):
        # Sanitize every log entry, including batch failures and provider errors.
        text = str(text)
        for profile in self.profiles.values():
            secret = profile.get("api_key", "")
            if secret:
                text = text.replace(secret, "[已隐藏密钥]")
        if self.key_var.get():
            text = text.replace(self.key_var.get(), "[已隐藏密钥]")
        if len(text) > LOG_ENTRY_CHARACTERS:
            text = text[:LOG_ENTRY_CHARACTERS] + "\n（过长日志已省略；任务内容保存在校对项目中。）"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        # Keep the visible history bounded; translation progress remains in the
        # project cache, independent of this text widget.
        lines = int(self.log_text.index("end-1c").split(".")[0]) - 1
        if lines > LOG_LINES:
            self.log_text.delete("1.0", f"{lines - LOG_LINES + 1}.0")
        characters = self.log_text.count("1.0", "end-1c", "chars")[0]
        if characters > LOG_CHARACTERS:
            self.log_text.delete("1.0", f"1.0+{characters - LOG_CHARACTERS}c")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.status.set(text[:110])

    def _open_project(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(title="打开校对项目", filetypes=[("翻译项目", "*.ptrans.json")])
        if not path:
            return
        try:
            project = Project.load(path)
            self._accept_paths([project.source])
            self.projects[project.source] = (project, Path(path))
            self._show_project(project, Path(path))
            self._load_options(project.options)
            self.notebook.select(self.review_tab)
        except (ValueError, OSError, TypeError) as error:
            messagebox.showerror("打开失败", str(error), parent=self.root)

    def _load_selected(self):
        if self.busy:
            return
        selected = self.file_tree.selection()
        if not selected:
            return
        source = self.files[int(selected[0])]
        if source not in self.projects:
            cache = cached_project_path(source, self.output_var.get())
            path = cache if cache.exists() else project_path(source, self.output_var.get())
            if path.exists():
                try:
                    self.projects[source] = (Project.load(path), path)
                except (ValueError, OSError) as error:
                    messagebox.showerror("载入失败", str(error), parent=self.root)
                    return
            else:
                messagebox.showinfo("尚未分析", "请先分析版面。", parent=self.root)
                return
        self._show_project(*self.projects[source])
        self._load_options(self.project.options)
        self.notebook.select(self.review_tab)

    def _show_project(self, project, path):
        self.project, self.project_file = project, Path(path)
        self.review_title.set(Path(project.source).name)
        self.page = min(self.page, max(0, len(project.pages) - 1))
        self._fill_blocks()
        self._schedule_preview()

    def _fill_blocks(self):
        self.block_tree.delete(*self.block_tree.get_children())
        if not self.project:
            return
        names = {"pending": "待翻译", "translated": "已翻译", "edited": "已修改", "preserved": "保留原文", "failed": "失败", "overflow": "排版溢出"}
        figures = {u.id: u for u in self.project.units if u.kind == "figure"}
        for b in self.project.blocks:
            review = bool(b.notes and not b.approved) or b.status in ("failed", "overflow")
            mode = self.filter_var.get()
            if mode == "待校对" and not review or mode == "可翻译" and b.protected or mode == "已翻译" and b.status not in ("translated", "edited"):
                continue
            state = "待校对" if review else names.get(b.status, b.status)
            parent = ""
            if b.unit_id in figures:
                unit = figures[b.unit_id]
                parent = unit.id
                if not self.block_tree.exists(parent):
                    members = [x for x in self.project.blocks if x.id in unit.block_ids]
                    done = sum(x.status in ("translated", "edited", "preserved") for x in members)
                    self.block_tree.insert("", "end", iid=parent, values=(unit.page + 1, f"整图 · {unit.title}", f"{done}/{len(members)}"))
            label = ("图题 · " if b.role == "figure_caption" else "标签 · " if parent else "段落 · " if b.role == "paragraph" else "") + b.text[:75]
            self.block_tree.insert(parent, "end", iid=b.id, values=(b.page + 1, label, state))
        for unit in figures.values():
            if not unit.block_ids and not self.block_tree.exists(unit.id) and self.filter_var.get() == "全部":
                self.block_tree.insert("", "end", iid=unit.id, values=(unit.page + 1, f"整图 · {unit.title}", "未识别标签"))

    def _selected_block(self):
        selected = self.block_tree.selection()
        if self.project and selected:
            return next((b for b in self.project.blocks if b.id == selected[0]), None)
        return None

    def _selected_unit(self):
        selected = self.block_tree.selection()
        if self.project and selected:
            return next((u for u in self.project.units if u.id == selected[0] and u.kind == "figure"), None)
        return None

    def _select_block(self, event=None):
        if self.busy:
            return
        block = self._selected_block()
        if not block:
            unit = self._selected_unit()
            if unit:
                members = [b for identity in unit.block_ids for b in self.project.blocks if b.id == identity]
                for widget, values in ((self.source_text, [b.text for b in members]), (self.translation_text, [b.translation or "（待翻译）" for b in members])):
                    widget.configure(state="normal")
                    widget.delete("1.0", "end")
                    widget.insert("1.0", "\n\n".join(values))
                    widget.configure(state="disabled")
                self.page = unit.page
                self.note_var.set("当前为整图总览；展开图片可分别校对图题与标签。翻译结合整张图，标签按原位置及方向显示。")
                self._schedule_preview()
            return
        for widget, value in [(self.source_text, block.text), (self.translation_text, block.translation)]:
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", value)
        self.keep_var.set(block.protected)
        self.approved_var.set(block.approved)
        self.angle_var.set(f"{block.angle:g}")
        self.vertical_var.set(block.vertical)
        self.note_var.set(("\n".join(block.notes) or "未检测到自动校对提示，仍建议对照原文检查。") + f"\n区域：{block.kind} · 字号 {block.fontsize:.1f} pt")
        if block.inline:
            self.note_var.set(self.note_var.get() + "\n段内 [[MATH_数字]] 代表原公式；编辑译文时保留这些标记，导出后显示原公式。")
        self.page = block.page
        self._schedule_preview()

    def _save_block(self):
        if self.busy:
            return
        block = self._selected_block()
        if not block:
            return
        try:
            angle = float(self.angle_var.get())
            if not math.isfinite(angle):
                raise ValueError
        except ValueError:
            messagebox.showerror("方向错误", "方向应为有限的角度数值。", parent=self.root)
            return
        source = self.source_text.get("1.0", "end-1c").strip()
        translated = self.translation_text.get("1.0", "end-1c").strip()
        if not source:
            messagebox.showerror("原文为空", "请填写原文。", parent=self.root)
            return
        corrected_source = source != block.text
        if corrected_source and block.inline:
            messagebox.showerror("段内含公式", "此段含原始公式。请保持原文字段不变，在译文字段中修改文字并保留公式标记。", parent=self.root)
            return
        old_translation = block.translation
        block.text, block.translation = source, translated
        block.angle, block.vertical = angle % 360, self.vertical_var.get()
        block.protected, block.approved = self.keep_var.get(), self.approved_var.get()
        block.status = "preserved" if block.protected else "edited"
        if not block.protected and (not translated or (corrected_source and translated == old_translation)):
            block.translation = ""
            block.status = "pending"
        if block.protected:
            block.translation = block.text
        self.project.save(self.project_file)
        self._fill_blocks()
        self.block_tree.selection_set(block.id)
        self._log("区域修改已保存；重新导出后，右侧 PDF 预览会更新。")

    def _export_current(self):
        if self.busy or not self.project:
            return
        try:
            options = self._options()
        except ValueError as error:
            messagebox.showerror("设置错误", str(error), parent=self.root)
            return
        directory = Path(self.project.output).parent if self.project.output else (
            Path(self.output_var.get()) if self.output_var.get().strip() else Path(self.project.source).parent / "译文")
        filename = filedialog.asksaveasfilename(title="导出翻译 PDF", initialfile=Path(self.project.source).stem + ".译文.pdf", defaultextension=".pdf", filetypes=[("PDF", "*.pdf")], initialdir=str(directory))
        if not filename:
            return
        if not self._save_all(show=False, validate=False):
            return
        self._launch_process({"kind": "export", "project_path": str(self.project_file),
                              "output": filename, "options": vars(options)})

    def _change_page(self, delta):
        if self.busy or not self.project:
            return
        self.page = min(max(0, self.page + delta), len(self.project.pages) - 1)
        self._schedule_preview()

    def _schedule_preview(self, delay=70):
        if self.preview_after is not None:
            self.root.after_cancel(self.preview_after)
            self.preview_after = None
        if self.closing or self.busy or self.notebook.select() != str(self.review_tab):
            return
        self.preview_after = self.root.after(delay, self._render_preview)

    def _render_preview(self):
        if self.preview_after is not None:
            self.root.after_cancel(self.preview_after)
            self.preview_after = None
        if self.busy or self.closing or not self.project or self.preview_rendering:
            return
        self.preview_rendering = True
        try:
            self._draw_preview()
        finally:
            self.preview_rendering = False

    def _draw_preview(self):
        selected = self._selected_block() or self._selected_unit()
        for i, canvas in enumerate(self.canvases):
            path = self.project.source if i == 0 or not self.project.output or not Path(self.project.output).exists() else self.project.output
            try:
                stat = Path(path).stat()
                key = (path, stat.st_mtime_ns, stat.st_size, self.page, max(280, canvas.winfo_width()), self.zoom_var.get())
                # Selecting another region only changes its outline. Retain the
                # two current page images until a replacement is ready.
                if self.preview_keys[i] == key:
                    self._draw_selection(canvas, i, selected)
                    continue
                with fitz.open(path) as document:
                    page = document[self.page]
                    fit_scale = max(0.15, (max(280, canvas.winfo_width()) - 20) / page.rect.width)
                    scale = fit_scale * float(self.zoom_var.get().rstrip("%")) / 100
                    scale = min(scale, PREVIEW_EDGE / max(page.rect.width, page.rect.height),
                                math.sqrt(PREVIEW_PIXELS / (page.rect.width * page.rect.height)))
                    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                    with Image.frombytes("RGB", (pix.width, pix.height), pix.samples) as image:
                        photo = ImageTk.PhotoImage(image, master=self.root)
                    canvas.delete("all")
                    canvas.create_image(10, 10, image=photo, anchor="nw", tags="page")
                    self.tk_images[i] = photo
                    self.preview_keys[i] = key
                    self.preview_scales[i] = (scale, tuple(page.rotation_matrix))
                    canvas.configure(scrollregion=(0, 0, pix.width + 20, pix.height + 20))
                    if i == 0:
                        self.preview_scale = scale
                    self._draw_selection(canvas, i, selected)
                self.page_label.set(f"{self.page + 1} / {len(self.project.pages)}")
            except (OSError, RuntimeError, ValueError, MemoryError, tk.TclError) as error:
                canvas.delete("all")
                self.tk_images[i] = self.preview_keys[i] = None
                canvas.create_text(20, 30, text="预览不可用：" + str(error), anchor="nw", width=280)

    def _draw_selection(self, canvas, index, selected):
        canvas.delete("selection")
        if selected and selected.page == self.page:
            scale, rotation = self.preview_scales[index]
            rect = fitz.Rect(selected.rect) * fitz.Matrix(rotation)
            review = any(b.notes and not b.approved for b in self.project.blocks
                         if b.id == selected.id or b.unit_id == selected.id)
            canvas.create_rectangle(rect.x0 * scale + 10, rect.y0 * scale + 10,
                                    rect.x1 * scale + 10, rect.y1 * scale + 10,
                                    outline="#e18a16" if review else BLUE, width=2, tags="selection")

    def _wheel(self, event):
        for canvas in self.canvases:
            canvas.yview_scroll(-int(event.delta / 120), "units")

    def _manual_toggle(self):
        if self.busy or not self.project:
            return
        self.manual_mode = not self.manual_mode
        self.canvases[0].configure(cursor="crosshair" if self.manual_mode else "")
        self._log("在左侧原文预览中拖动框选漏识别的文字区域。" if self.manual_mode else "已退出框选模式。")

    def _manual_start(self, event):
        if not self.manual_mode or self.busy:
            return
        canvas = self.canvases[0]
        self.selection_start = (canvas.canvasx(event.x), canvas.canvasy(event.y))
        canvas.delete("manual")

    def _manual_drag(self, event):
        if not self.selection_start:
            return
        canvas = self.canvases[0]
        canvas.delete("manual")
        canvas.create_rectangle(*self.selection_start, canvas.canvasx(event.x), canvas.canvasy(event.y), outline=BLUE, width=2, tags="manual")

    def _manual_finish(self, event):
        if not self.selection_start or self.busy:
            return
        canvas = self.canvases[0]
        x0, y0 = self.selection_start
        x1, y1 = canvas.canvasx(event.x), canvas.canvasy(event.y)
        self.selection_start = None
        rect = fitz.Rect((min(x0, x1) - 10) / self.preview_scale, (min(y0, y1) - 10) / self.preview_scale,
                         (max(x0, x1) - 10) / self.preview_scale, (max(y0, y1) - 10) / self.preview_scale)
        with fitz.open(self.project.source) as document:
            page = document[self.page]
            rect = rect * page.derotation_matrix
            page.set_rotation(0)
            rect &= page.rect
        if rect.width < 2 or rect.height < 2:
            return
        text = simpledialog.askstring("补充文字区域", "输入框选区域的原文（稍后可在编辑器中填写译文）：", parent=self.root)
        if not text or not text.strip():
            return
        number = 1
        ids = {b.id for b in self.project.blocks}
        while f"p{self.page + 1}-m{number}" in ids:
            number += 1
        b = Block(id=f"p{self.page + 1}-m{number}", page=self.page, rect=list(rect), text=text.strip(), kind="raster",
                  quad=[[rect.x0, rect.y0], [rect.x1, rect.y0], [rect.x1, rect.y1], [rect.x0, rect.y1]],
                  fontsize=min(12, rect.height * 0.75), local_width=rect.width, local_height=rect.height)
        self.project.blocks.append(b)
        self.project.save(self.project_file)
        self._fill_blocks()
        self.block_tree.selection_set(b.id)
        self.block_tree.see(b.id)
        self.manual_mode = False
        canvas.configure(cursor="")

    def _import_glossary(self):
        path = filedialog.askopenfilename(title="导入术语表（前两列为原词、译词）", filetypes=[("术语表", "*.csv *.tsv")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle, delimiter="\t" if path.lower().endswith(".tsv") else ","))
            result = []
            for i, row in enumerate(rows):
                if len(row) < 2:
                    continue
                if i == 0 and row[0].strip().lower() in ("source", "原文", "原词", "term"):
                    continue
                if row[0].strip() and row[1].strip():
                    result.append(row[0].strip() + "=" + row[1].strip())
            self.glossary_text.insert("end", ("\n" if self.glossary_text.get("1.0", "end").strip() else "") + "\n".join(result))
            self._log(f"已导入 {len(result)} 条术语。")
        except (OSError, UnicodeError, csv.Error) as error:
            messagebox.showerror("导入失败", str(error), parent=self.root)

    def _about(self):
        messagebox.showinfo("文献译境", f"PaperTranslator {__version__}\nPython · Tk · PyMuPDF · RapidOCR\n\n本项目按 AGPL-3.0-only 提供；源码、第三方许可与构建说明见：\n{PROJECT_URL}\n各使用者自行配置 API；软件没有内置共享密钥。\n\n保留原页数、位置及方向。疑难 OCR、竖排和复杂背景需校对。\n首次使用请先处理 1–2 页，再检查图表和公式。\n详细规则、限制与构建方法见 README.md。", parent=self.root)

    def _close(self):
        if self.busy:
            if not messagebox.askyesno("取消并退出", "任务正在运行。是否取消任务、保存进度后退出？", parent=self.root):
                return
            self.closing = True
            self._cancel()
        else:
            self._destroy()

    def _destroy(self):
        self.closing = True
        for attribute in ("preview_after", "poll_after"):
            callback = getattr(self, attribute, None)
            if callback is not None:
                self.root.after_cancel(callback)
                setattr(self, attribute, None)
        self.root.destroy()
