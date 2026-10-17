from __future__ import annotations

from pathlib import Path
import queue
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from papertranslator.gui import App
from papertranslator.models import Options, Project
from papertranslator.worker import execute


class WorkerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {"PAPERTRANSLATOR_DATA_DIR": directory.name})
        environment.start()
        self.addCleanup(environment.stop)

    def request(self, **changes):
        request = {"kind": "queue", "mode": "analyze", "files": ["fixture.pdf"],
                   "output": "unused", "options": vars(Options(ocr=False, translate_figures=False)),
                   "provider": {"api_key": "fake-test-secret"}, "autoexport": False}
        return request | changes

    def drain(self, events):
        result = []
        while not events.empty():
            result.append(events.get_nowait())
        return result

    def test_event_burst_yields_to_windows_after_100_messages(self):
        app = Mock()
        app.events = queue.Queue()
        for number in range(250):
            app.events.put(("log", str(number)))
        with patch("papertranslator.gui.time.monotonic", return_value=0):
            App._poll(app)
        self.assertEqual(app._log.call_count, 100)
        self.assertEqual(app.events.qsize(), 150)
        app.root.after.assert_called_once_with(10, app._poll)

    def test_completed_export_does_not_mask_next_file_analysis_status(self):
        app = Mock()
        app.busy = True
        App._export_update(app, {"phase": "complete", "category": "export"})
        self.assertIsNone(app.export_progress)
        app.force_export_button.configure.assert_called_once_with(state="disabled")
        app.progress.configure.assert_called_once_with(mode="indeterminate", maximum=100, value=0)

    def test_cancel_before_analysis_does_not_open_pdf(self):
        cancel, events = threading.Event(), queue.Queue()
        cancel.set()
        with patch("papertranslator.engine.Engine") as factory:
            execute(self.request(), cancel, events)
        factory.return_value.analyze.assert_not_called()
        messages = self.drain(events)
        self.assertTrue(any("已取消" in value for kind, value in messages if kind == "log"))
        self.assertEqual(messages[-1], ("complete", None))

    def test_worker_error_redacts_key_before_ipc(self):
        events = queue.Queue()
        with patch("papertranslator.engine.Engine") as factory:
            factory.return_value.analyze.side_effect = RuntimeError("bad fake-test-secret")
            execute(self.request(), threading.Event(), events)
        messages = self.drain(events)
        self.assertTrue(any(kind == "error" for kind, _ in messages))
        self.assertNotIn("fake-test-secret", repr(messages))
        self.assertIn("[已隐藏密钥]", repr(messages))
        self.assertEqual(messages[-1], ("complete", None))

    def test_queue_continues_after_one_file_fails_and_sends_saved_path(self):
        events = queue.Queue()
        project = Project("second.pdf", "", {})
        with patch("papertranslator.engine.Engine") as factory, patch.object(Project, "save") as save:
            factory.return_value.analyze.side_effect = [ValueError("bad PDF"), project]
            execute(self.request(files=["first.pdf", "second.pdf"]), threading.Event(), events)
        messages = self.drain(events)
        self.assertIn(("file_state", ("first.pdf", "失败，可重试")), messages)
        self.assertIn(("file_state", ("second.pdf", "已分析，可翻译")), messages)
        snapshots = [value for kind, value in messages if kind == "project_path"]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0][0], "second.pdf")
        save.assert_called_once_with(Path(snapshots[0][1]))

    def test_failed_export_saves_progress_before_reporting_error(self):
        project, events = Project("fixture.pdf", "", {}), queue.Queue()
        request = self.request(kind="export", project_path="saved.ptrans.json", output="translated.pdf")
        with patch("papertranslator.engine.Engine") as factory, \
                patch.object(Project, "load", return_value=project), patch.object(Project, "save") as save:
            factory.return_value.export.side_effect = ValueError("cancelled export fixture")
            execute(request, threading.Event(), events)
        messages = self.drain(events)
        save.assert_called_once_with(Path("saved.ptrans.json"))
        self.assertIn(("project_path", ("fixture.pdf", "saved.ptrans.json")), messages)
        self.assertIn(("error", "cancelled export fixture"), messages)

    def test_translation_and_autoexport_use_original_output_identity(self):
        project, events = Project("fixture.pdf", "", {}), queue.Queue()
        with patch("papertranslator.engine.Engine") as factory, patch.object(Project, "save"):
            engine = factory.return_value
            engine.analyze.return_value = project
            engine.export.return_value = {"needs_review": [], "warnings": []}
            execute(self.request(mode="translate", autoexport=True), threading.Event(), events)
        engine.translate.assert_called_once()
        engine.export.assert_called_once()
        output = engine.export.call_args.args[1]
        self.assertTrue(output.name.endswith(".paragraphs.译文.pdf"))
        self.assertEqual(output.parent, Path("unused"))
        self.assertIn(("file_state", ("fixture.pdf", "已导出")), self.drain(events))


if __name__ == "__main__":
    unittest.main()
