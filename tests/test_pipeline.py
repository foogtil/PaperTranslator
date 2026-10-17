from __future__ import annotations

import json
from pathlib import Path
import tempfile
import os
import threading
import unittest
from unittest.mock import patch, Mock

import httpx
import pymupdf as fitz

from papertranslator.api import APIError, ChatClient, endpoint
from papertranslator.extraction import analyze, inverse_rotated_point
from papertranslator.layout import export_pdf
from papertranslator.models import Block, Cancelled, Options, Project, Provider, parse_pages
from papertranslator.selftest import make_fixture, mock_transport, run_checks
from papertranslator.translation import protect, restore, segment_results, translate_project, validate_segments


class TranslationTests(unittest.TestCase):
    def response_client(self):
        client = Mock()
        client.provider = Provider(api_key="offline-only")
        client.cancel = threading.Event()
        return client

    def small_project(self, texts):
        options = Options(ocr=False)
        blocks = [Block(f"b{i}", 0, [10, 10 + 20 * i, 200, 25 + 20 * i], text) for i, text in enumerate(texts)]
        return Project("offline", "", vars(options), blocks=blocks), options

    def test_partial_success_retries_only_empty_region(self):
        project, options = self.small_project(["Alpha text", "Beta text", "Gamma text"])
        client = self.response_client()
        client.complete.side_effect = [
            '{"segments":[{"id":"b0","translation":"甲"},{"id":"b1","translation":""},{"id":"b2","translation":"丙"}]}',
            '{"segments":[{"id":"b1","translation":"乙"}]}',
        ]
        snapshots = []
        translate_project(project, options, client, lambda: snapshots.append([b.translation for b in project.blocks]), lambda t: None)
        self.assertEqual(client.complete.call_count, 2)
        retry = json.loads(client.complete.call_args_list[1].args[0][1]["content"])
        self.assertEqual([s["id"] for s in retry["segments"]], ["b1"])
        self.assertIn(["甲", "", "丙"], snapshots)
        self.assertEqual([b.translation for b in project.blocks], ["甲", "乙", "丙"])

    def test_empty_region_retries_are_bounded(self):
        project, options = self.small_project(["A scientific phrase"])
        client = self.response_client()
        client.complete.return_value = '{"segments":[{"id":"b0","translation":""}]}'
        logs = []
        translate_project(project, options, client, lambda: None, logs.append)
        self.assertEqual(client.complete.call_count, 3)
        self.assertEqual(project.blocks[0].status, "failed")
        self.assertFalse(project.blocks[0].translation)
        self.assertTrue(any("A scientific phrase" in entry and "b0" in entry for entry in logs))

    def test_placeholder_error_does_not_discard_neighbour(self):
        project, options = self.small_project(["Use 12 values", "Other text"])
        client = self.response_client()
        client.complete.side_effect = [
            '{"segments":[{"id":"b0","translation":"使用数值"},{"id":"b1","translation":"其他文字"}]}',
            '{"segments":[{"id":"b0","translation":"使用 [[KEEP_0]] 个数值"}]}',
        ]
        translate_project(project, options, client, lambda: None, lambda t: None)
        retry = json.loads(client.complete.call_args_list[1].args[0][1]["content"])
        self.assertEqual([s["id"] for s in retry["segments"]], ["b0"])
        self.assertIn("12", project.blocks[0].translation)
        self.assertEqual(project.blocks[1].translation, "其他文字")

    def test_partial_results_survive_cancellation(self):
        project, options = self.small_project(["Alpha text", "Beta text"])
        client = self.response_client()
        client.complete.return_value = '{"segments":[{"id":"b0","translation":"甲"},{"id":"b1","translation":""}]}'
        def save():
            if project.blocks[0].translation:
                client.cancel.set()
        with self.assertRaises(Cancelled):
            translate_project(project, options, client, save, lambda t: None)
        self.assertEqual(project.blocks[0].translation, "甲")
        self.assertEqual(project.blocks[0].status, "translated")
        self.assertEqual(client.complete.call_count, 1)

    def test_exhausted_network_is_not_bisected(self):
        project, options = self.small_project(["Alpha text", "Beta text"])
        client = self.response_client()
        client.complete.side_effect = APIError("网络重试耗尽", kind="network")
        with self.assertRaises(APIError):
            translate_project(project, options, client, lambda: None, lambda t: None)
        self.assertEqual(client.complete.call_count, 1)

    def test_missing_null_and_duplicate_results_are_independent(self):
        raw = '{"segments":[{"id":"a","translation":"正确"},{"id":"b","translation":null},{"id":"c","preserve":false}]}'
        accepted, errors = segment_results(raw, ["a", "b", "c", "d"])
        self.assertEqual(set(accepted), {"a"})
        self.assertIn("NoneType", errors["b"])
        self.assertIn("缺少", errors["c"])
        self.assertIn("没有返回", errors["d"])
        accepted, errors = segment_results('{"segments":[{"id":"a","translation":"甲"},{"id":"a","translation":"乙"},{"id":"b","translation":"丙"}]}', ["a", "b"])
        self.assertEqual(set(accepted), {"b"})
        self.assertIn("重复", errors["a"])

    def test_preserve_true_needs_no_translation_text(self):
        project, options = self.small_project(["Identifier U12"])
        client = self.response_client()
        client.complete.return_value = '{"segments":[{"id":"b0","translation":"","preserve":true}]}'
        translate_project(project, options, client, lambda: None, lambda t: None)
        self.assertEqual(client.complete.call_count, 1)
        self.assertEqual(project.blocks[0].translation, "Identifier U12")
        self.assertEqual(project.blocks[0].status, "preserved")
        self.assertTrue(project.blocks[0].notes)

    def test_font_changes_do_not_split_prose_into_words(self):
        from papertranslator.extraction import native_blocks
        with fitz.open() as document:
            page = document.new_page()
            for y, text in [(70, "The original data captures the structure."), (85, "The representation remains stable.")]:
                x = 40
                for i, word in enumerate(text.split()):
                    font = "helv" if i % 2 else "hebo"
                    page.insert_text((x, y), word, fontname=font, fontsize=11)
                    x += fitz.get_text_length(word + " ", fontname=font, fontsize=11)
            prose = [b for b in native_blocks(page, 0) if not b.protected]
            self.assertLessEqual(len(prose), 2)
            self.assertIn("original data captures", " ".join(b.text for b in prose))

    def test_table_cells_remain_separate(self):
        from papertranslator.extraction import native_blocks
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((40, 70), "Algorithm", fontsize=11)
            page.insert_text((320, 70), "Accuracy", fontsize=11)
            text = [b.text for b in native_blocks(page, 0)]
            self.assertIn("Algorithm", text)
            self.assertIn("Accuracy", text)

    def test_ligature_fragments_join_into_one_word(self):
        from papertranslator.extraction import _run_text
        line = {"dir": [1, 0]}
        spans = [{"bbox": box, "size": 10, "ascender": 1, "descender": -0.2} for box in ([0, 0, 10, 12], [10, 0, 15, 12], [15, 0, 40, 12])]
        runs = [(line, span, text, False) for span, text in zip(spans, ("de", "ﬁ", "nition"))]
        self.assertEqual(_run_text(runs), "definition")

    def test_math_font_is_an_inline_object_in_complete_paragraph(self):
        from papertranslator.extraction import native_blocks
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((40, 70), "Consider", fontsize=11)
            page.insert_text((90, 70), "a", fontsize=11, fontname="symb")
            page.insert_text((104, 70), "in the proof.", fontsize=11)
            blocks = native_blocks(page, 0)
            paragraph = next(b for b in blocks if "Consider" in b.text)
            self.assertIn("proof", paragraph.text)
            self.assertEqual(len(paragraph.inline), 1)
            self.assertIn("[[MATH_0]]", paragraph.template)
            self.assertNotIn("proof", paragraph.inline[0]["text"])

    def test_legacy_project_keeps_existing_progress(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source, path = root / "original.pdf", root / "project.json"
            make_fixture(source)
            options = Options(ocr=False)
            project = analyze(str(source), options, str(path), threading.Event(), lambda t: None)
            block = next(b for b in project.blocks if not b.protected)
            block.translation, block.status = "保留人工校对结果", "edited"
            project.save(path)
            old = json.loads(path.read_text(encoding="utf-8"))
            old.pop("extraction_version")
            path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
            logs = []
            resumed = analyze(str(source), options, str(path), threading.Event(), logs.append)
            self.assertEqual([b.id for b in resumed.blocks], [b.id for b in project.blocks])
            self.assertEqual(next(b.translation for b in resumed.blocks if b.id == block.id), "保留人工校对结果")
            self.assertTrue(any("旧版分块" in entry for entry in logs))

    def test_exact_number_formula_tokens(self):
        source = "Use 1.25e-3, 95.5%, [12-14], $x_i + 1$ and https://example.org."
        value, tokens = protect(source)
        self.assertEqual(restore(value, tokens), source)
        with self.assertRaises(APIError):
            restore(value.replace("[[KEEP_0]]", "12"), tokens)
        with self.assertRaises(APIError):
            restore(value + "[[KEEP_0]]", tokens)

    def test_segment_mismatch_rejected(self):
        with self.assertRaises(APIError):
            validate_segments('{"segments":[{"id":"extra","translation":"值"}]}', ["a"])
        with self.assertRaises(APIError):
            validate_segments('{"segments":[]}', ["a"])

    def test_page_ranges(self):
        self.assertEqual(parse_pages("1-3,5,2", 5), [0, 1, 2, 4])
        for bad in ("0", "3-2", "6", "1--2", "1,"):
            with self.assertRaises(ValueError):
                parse_pages(bad, 5)

    def test_safe_api_address(self):
        self.assertEqual(endpoint("https://example.com/v1/"), "https://example.com/v1/chat/completions")
        self.assertEqual(endpoint("http://127.0.0.1:8000/v1"), "http://127.0.0.1:8000/v1/chat/completions")
        for url in ("http://remote.example/v1", "https://secret@host/v1", "https://host/v1?key=secret"):
            with self.assertRaises(ValueError):
                endpoint(url)

    def test_authentication_failure_does_not_retry(self):
        calls = []
        def reject(request):
            calls.append(request)
            return httpx.Response(401, text="bad key")
        client = ChatClient(Provider(api_key="secret-test-value"), transport=httpx.MockTransport(reject))
        with self.assertRaises(APIError):
            client.test()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].headers["authorization"], "Bearer secret-test-value")
        client.close()

    def test_mimo_header_and_sse(self):
        def check(request):
            self.assertEqual(request.headers["api-key"], "secret-test-value")
            self.assertNotIn("authorization", request.headers)
            return mock_transport(request)
        client = ChatClient(Provider(api_key="secret-test-value", auth_header="api-key"), transport=httpx.MockTransport(check))
        self.assertEqual(client.test(), "OK")
        client.close()

    def test_cancel_before_request(self):
        cancel = threading.Event()
        cancel.set()
        client = ChatClient(Provider(api_key="secret-test-value"), cancel, transport=httpx.MockTransport(mock_transport))
        with self.assertRaises(Cancelled):
            client.test()
        client.close()

    def test_rotation_mapping(self):
        self.assertEqual(inverse_rotated_point(30, 70, 100, 200, 90), (30, 30))
        self.assertEqual(inverse_rotated_point(30, 70, 100, 200, 180), (70, 130))
        self.assertEqual(inverse_rotated_point(30, 70, 100, 200, 270), (70, 170))

    def test_native_export_roundtrip(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            result = run_checks(directory, include_ocr=False, include_gui=False)
            self.assertTrue(result["ok"], result)

    def test_overflow_keeps_source(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = root / "original.pdf"
            with fitz.open() as doc:
                page = doc.new_page()
                page.insert_text((50, 50), "Short label", fontsize=12)
                doc.save(source)
            options = Options(ocr=False, minimum_font=12)
            project = analyze(str(source), options, str(root / "project.json"), threading.Event(), lambda t: None)
            block = project.blocks[0]
            block.translation = "这是非常长的译文。" * 500
            block.status = "translated"
            report = export_pdf(project, root / "output.pdf", options)
            self.assertIn(block.id, report["needs_review"])
            with fitz.open(root / "output.pdf") as output:
                self.assertIn("Short label", output[0].get_text())

    def test_resume_and_model_change(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = root / "original.pdf"
            make_fixture(source)
            options = Options(ocr=False)
            path = root / "project.json"
            project = analyze(str(source), options, str(path), threading.Event(), lambda t: None)
            calls = []
            def call(request):
                calls.append(request)
                return mock_transport(request)
            client = ChatClient(Provider(api_key="secret-test-value"), transport=httpx.MockTransport(call))
            translate_project(project, options, client, lambda: project.save(path), lambda t: None)
            count = len(calls)
            translate_project(project, options, client, lambda: project.save(path), lambda t: None)
            self.assertEqual(count, len(calls))
            client.provider.model = "changed-model"
            translate_project(project, options, client, lambda: project.save(path), lambda t: None)
            self.assertGreater(len(calls), count)
            self.assertNotIn("secret-test-value", path.read_text(encoding="utf-8"))
            client.close()

    def test_changed_source_detected(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = root / "original.pdf"
            make_fixture(source)
            path = root / "project.json"
            analyze(str(source), Options(ocr=False), str(path), threading.Event(), lambda t: None)
            source.write_bytes(source.read_bytes() + b"\n% changed\n")
            with self.assertRaises(ValueError):
                Project.load(path)

    def test_truncated_stream_rejected(self):
        def broken(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text='data: {"choices":[{"delta":{"content":"OK"}}]}\n\n')
        client = ChatClient(Provider(api_key="test-secret"), transport=httpx.MockTransport(broken))
        with self.assertRaises(APIError):
            client.test()
        client.close()

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_encrypted_settings(self):
        from papertranslator.settings import load_settings, save_settings
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            old = os.environ.get("PAPERTRANSLATOR_DATA_DIR")
            os.environ["PAPERTRANSLATOR_DATA_DIR"] = directory
            try:
                data = {"profiles": {"DeepSeek": {"api_key": "fake-secret-for-dpapi-test"}}}
                save_settings(data, remember=True)
                self.assertNotIn("fake-secret-for-dpapi-test", (Path(directory) / "settings.json").read_text())
                self.assertEqual(load_settings()["profiles"]["DeepSeek"]["api_key"], "fake-secret-for-dpapi-test")
                save_settings(data, remember=False)
                self.assertNotIn("key_dpapi", (Path(directory) / "settings.json").read_text())
            finally:
                if old is None:
                    os.environ.pop("PAPERTRANSLATOR_DATA_DIR", None)
                else:
                    os.environ["PAPERTRANSLATOR_DATA_DIR"] = old

    def test_existing_redaction_is_not_applied(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = root / "original.pdf"
            with fitz.open() as doc:
                p = doc.new_page()
                p.insert_text((50, 70), "Keep this source", fontsize=12)
                p.add_redact_annot(fitz.Rect(300, 200, 400, 250))
                doc.save(source)
            options = Options(ocr=False)
            project = analyze(str(source), options, str(root / "project.json"), threading.Event(), lambda t: None)
            project.blocks[0].translation = "保留原始内容"
            report = export_pdf(project, root / "output.pdf", options)
            self.assertTrue(report["needs_review"])
            with fitz.open(root / "output.pdf") as doc:
                self.assertIn("Keep this source", doc[0].get_text())
                self.assertTrue(any(a.type[0] == fitz.PDF_ANNOT_REDACT for a in doc[0].annots()))

    def test_vertical_columns_fit_and_search(self):
        from papertranslator.models import Block, PageInfo, digest_file
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = root / "original.pdf"
            with fitz.open() as doc:
                doc.new_page(width=300, height=400)
                doc.save(source)
            options = Options(ocr=False, minimum_font=10)
            block = Block("vertical", 0, [80, 80, 160, 250], "Vertical test", vertical=True, fontsize=12,
                          translation="计算机科学与数学的竖排文字测试" * 2, status="translated")
            project = Project(str(source), digest_file(source), vars(options), pages=[PageInfo(0, 300, 400, 0)], blocks=[block], selected_pages=[0])
            report = export_pdf(project, root / "output.pdf", options)
            self.assertFalse(report["needs_review"], report)
            with fitz.open(root / "output.pdf") as doc:
                self.assertIn("计", doc[0].get_text())

    def test_vision_regions_are_validated(self):
        from papertranslator.engine import Engine
        from papertranslator.models import PageInfo, digest_file
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = root / "original.pdf"
            with fitz.open() as doc:
                doc.new_page(width=300, height=400)
                doc.save(source)
            options = Options(ocr=False, vision_review=True)
            project = Project(str(source), digest_file(source), vars(options), pages=[PageInfo(0, 300, 400, 0, True)], selected_pages=[0])
            with patch("papertranslator.engine.ChatClient") as client_type:
                client = client_type.return_value
                client.provider = Provider(api_key="fake-key")
                client.cancel = threading.Event()
                client.read_layout.return_value = {"regions": [
                    {"text": "Accuracy", "bbox": [10, 10, 500, 120], "confidence": 0.99},
                    {"text": "Invalid", "bbox": [0, 0, float("nan"), 100]},
                    {"text": "Outside", "bbox": [-50, 0, 100, 100]},
                    {"text": "Duplicate", "bbox": [10, 10, 500, 120]},
                ]}
                client.read_crop.return_value = {"text": "Accuracy", "uncertain": False, "is_formula": False, "angle": 0}
                client.complete.return_value = '{"segments":[{"id":"p1-v1","translation":"准确率","preserve":false}]}'
                Engine().translate(project, options, Provider(api_key="fake-key"), root / "project.json")
            self.assertEqual(len(project.blocks), 1)
            self.assertEqual(project.blocks[0].translation, "准确率")
            self.assertEqual(project.blocks[0].rect, [3, 4, 150, 48])
            self.assertTrue(project.pages[0].vision_analyzed)


if __name__ == "__main__":
    unittest.main()
