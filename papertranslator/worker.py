"""Run PDF/OCR work outside the Tk process; pass no widgets or PDF objects."""
from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

from .models import Cancelled, Options, Project, Provider, checkpoint, dataclass_from


def execute(request, cancel, messages):
    # Imported only by the worker, so native PDF/OCR calls never hold the UI GIL.
    from .engine import Engine, estimate, project_path, resume_project_path

    provider = dataclass_from(Provider, request.get("provider", {}))

    def emit(kind, value):
        if kind in ("log", "error") and provider.api_key:
            value = str(value).replace(provider.api_key, "[已隐藏密钥]")
        messages.put((kind, value))

    engine = Engine(log=lambda text: emit("log", text), cancel=cancel,
                    progress=lambda data: emit("progress", data))
    options = dataclass_from(Options, request["options"])
    try:
        if request["kind"] == "export":
            path = Path(request["project_path"])
            project = Project.load(path)
            try:
                emit("project_path", (project.source, str(path)))
                report = engine.export(project, request["output"], options, path)
                emit("log", f"已导出 {request['output']}；{len(report['needs_review'])} 个区域需校对。")
            finally:
                project.save(path)
                emit("project_path", (project.source, str(path)))
            return
        if request["kind"] != "queue":
            raise ValueError("未知的后台任务。")
        files, output = request["files"], Path(request["output"])
        for number, source in enumerate(files, 1):
            checkpoint(cancel)
            emit("log", f"[{number}/{len(files)}] {Path(source).name}")
            artifact = project_path(source, str(output))
            path = resume_project_path(source, str(output))
            project = None
            try:
                project = engine.analyze(source, options, path)
                if request["mode"] == "analyze":
                    info = estimate(project, provider, options)
                    cost = f"{info['estimated_text_cost']:.4f} {info['currency']}" if info["estimated_text_cost"] is not None else "未设置单价"
                    emit("log", f"已分析 {len(project.selected_pages)} 页，{info['paragraphs']} 个段落、{info['figures']} 张图（含 {info['regions']} 个正文及标签项），约 {info['characters']} 字符；文本费用估算：{cost}。不含图片、重试及推理用量。")
                    state = "已分析，可翻译"
                else:
                    engine.translate(project, options, provider, path)
                    if request["autoexport"]:
                        out = output / (artifact.name.removesuffix(".ptrans.json") + ".译文.pdf")
                        emit("project_path", (source, str(path)))
                        report = engine.export(project, out, options, path)
                        review = len(report["needs_review"])
                        state = f"已导出 / {review} 待校对" if review else ("已导出 / 有警告" if report["warnings"] else "已导出")
                        emit("log", f"{state}：{out}")
                    else:
                        state = "已翻译，待导出"
                emit("file_state", (source, state))
            except Cancelled:
                emit("file_state", (source, "已暂停，可继续"))
                raise
            except Exception as error:
                emit("file_state", (source, "失败，可重试"))
                emit("log", f"处理失败 {Path(source).name}：{error}")
                if len(files) == 1:
                    raise
            finally:
                if project is not None:
                    project.save(path)
                    emit("project_path", (source, str(path)))
    except Cancelled as error:
        emit("log", str(error))
    except Exception as error:
        emit("error", str(error))
    finally:
        emit("complete", None)


class _MessageSender:
    def __init__(self, connection):
        self.connection = connection

    def put(self, message):
        self.connection.send(message)


class PDFWorker:
    def __init__(self, request):
        context = mp.get_context("spawn")
        self.cancel = context.Event()
        self.receiver, self.sender = context.Pipe(duplex=False)
        self.process = context.Process(target=execute, args=(request, self.cancel, _MessageSender(self.sender)),
                                       name="papertranslator-pdf", daemon=True)
        self.exporting = False
        self.forced = False
        self.latest_project = None
        self.temporary_output = None

    def start(self):
        self.process.start()
        # Only the child retains a writer. A crash or forced stop then produces
        # EOF even if it interrupts a message, instead of leaving a queue read
        # blocked on a partial frame with a parent-side writer still open.
        self.sender.close()

    def force_stop_export(self):
        if not self.exporting:
            return False
        try:
            if not self.process.is_alive():
                return False
            self.forced = True
            self.cancel.set()
            self.process.terminate()
            return True
        except (OSError, ValueError):
            self.forced = False
            return False

    def relay(self, events):
        """Only IPC runs in this thread; it never calls Tk or the PDF engine."""
        try:
            while True:
                try:
                    available = self.receiver.poll(0.1)
                    if available:
                        kind, value = self.receiver.recv()
                except (EOFError, OSError):
                    break
                if not available:
                    if not self.process.is_alive():
                        break
                    continue
                if kind == "project_path":
                    self.latest_project = value
                elif kind == "progress":
                    self.exporting = value.get("category") == "export" and value.get("phase") != "complete"
                    if value.get("temporary"):
                        self.temporary_output = value["temporary"]
                if kind != "complete":
                    events.put((kind, value))
            self.process.join()
            if self.forced:
                events.put(("log", "排版已强制停止；已保存的译文保留，可重新导出。"))
                if self.latest_project:
                    events.put(("project_path", self.latest_project))
            elif self.process.exitcode:
                events.put(("error", f"PDF 工作进程意外退出（代码 {self.process.exitcode}）。已保存的项目可继续。"))
        finally:
            self.exporting = False
            if self.forced and self.temporary_output:
                temporary = Path(self.temporary_output)
                if temporary.name.startswith(".papertranslator-") and temporary.suffix == ".pdf":
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        events.put(("log", "临时排版文件仍被占用，请稍后清理：" + str(temporary)))
            self.close()
            events.put(("idle", None))

    def close(self):
        self.sender.close()
        self.receiver.close()
        self.process.close()
