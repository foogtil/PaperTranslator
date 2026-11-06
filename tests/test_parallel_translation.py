from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
import pymupdf as fitz
from papertranslator.api import APIError, ChatClient
from papertranslator.engine import Engine
from papertranslator.models import Block, Cancelled, Options, Project, Provider, Unit
from papertranslator.translation import translate_project
from papertranslator.worker import _MessageSender


def fixture(count=8):
    options = Options(vision_review=False)
    blocks = [Block(f"b{i}", i, [0,0,100,100], f"Scientific paragraph {chr(65+i)}") for i in range(count)]
    return Project("offline", "", vars(options), blocks=blocks), options


def response(request):
    content = json.loads(request.content)["messages"][-1]["content"]
    if isinstance(content, list):
        content = content[0]["text"]
    segments = json.loads(content)["segments"]
    return httpx.Response(200, json={"choices":[{"message":{"content":json.dumps({"segments":[
        {"id": s["id"], "translation":"译文 " + s["id"]} for s in segments]})}, "finish_reason":"stop"}],
        "usage":{"prompt_tokens":5,"completion_tokens":3}})


class ParallelTranslationTests(unittest.TestCase):
    def test_parallel_changes_timing_only_and_obeys_limit(self):
        records, peaks = [], []
        for workers in (1, 2):
            project, options = fixture()
            options.translation_workers = workers
            lock = threading.Lock()
            active, peak, calls = 0, 0, {}
            def handle(request):
                nonlocal active, peak
                body = json.loads(request.content)
                identity = json.loads(body["messages"][-1]["content"])["segments"][0]["id"]
                with lock:
                    active += 1
                    peak = max(peak, active)
                    calls[identity] = body
                time.sleep(0.025)
                with lock:
                    active -= 1
                return response(request)
            client = ChatClient(Provider(api_key="offline", vision=False), cancel=threading.Event(), transport=httpx.MockTransport(handle))
            try: translate_project(project, options, client, lambda: None, lambda _: None)
            finally: client.close()
            records.append(calls)
            peaks.append(peak)
            self.assertTrue(all(b.translation == "译文 " + b.id for b in project.blocks))
        self.assertEqual(peaks, [1, 2])
        self.assertEqual(records[0], records[1])

    def test_engine_usage_and_atomic_snapshots_do_not_lose_updates(self):
        project, options = fixture(12)
        options.translation_workers = 4
        seen = []
        def handle(request):
            time.sleep(0.01)
            return response(request)
        def client_factory(provider, cancel, **kwargs):
            return ChatClient(provider, cancel, transport=httpx.MockTransport(handle), **kwargs)
        with tempfile.TemporaryDirectory() as directory, patch("papertranslator.engine.ChatClient", side_effect=client_factory):
            path = Path(directory)/"project.json"
            Engine(on_usage=seen.append).translate(project, options, Provider(api_key="offline", vision=False), path)
            saved = Project.load(path, verify=False)
        self.assertEqual(saved.usage["requests"], 12)
        self.assertEqual(saved.usage["input_tokens"], 60)
        self.assertEqual(saved.usage["output_tokens"], 36)
        self.assertEqual([s.get("requests",0) for s in seen], list(range(13)))
        self.assertTrue(all(b.status == "translated" for b in saved.blocks))

    def test_cancel_stops_new_requests_and_keeps_completed_translations(self):
        project, options = fixture()
        cancel, calls, snapshots = threading.Event(), [], []
        def handle(request):
            calls.append(request)
            time.sleep(0.02)
            return response(request)
        def save():
            snapshots.append([b.translation for b in project.blocks])
            if any(b.translation for b in project.blocks): cancel.set()
        client = ChatClient(Provider(api_key="offline"), cancel=cancel, transport=httpx.MockTransport(handle))
        try:
            with self.assertRaises(Cancelled): translate_project(project, options, client, save, lambda _: None)
            self.assertIs(client.cancel, cancel)
        finally: client.close()
        self.assertLessEqual(len(calls), 2)
        self.assertTrue(any(any(s) for s in snapshots))

    def test_fatal_error_aborts_pending_work_without_cancelling_next_document(self):
        project, options = fixture()
        cancel, calls = threading.Event(), []
        def handle(request):
            calls.append(request)
            return httpx.Response(401, text="offline rejection")
        client = ChatClient(Provider(api_key="offline"), cancel=cancel, transport=httpx.MockTransport(handle))
        try:
            with self.assertRaises(APIError): translate_project(project, options, client, lambda: None, lambda _: None)
            self.assertIs(client.cancel, cancel)
            self.assertFalse(cancel.is_set())
        finally: client.close()
        self.assertLessEqual(len(calls), 2)

    def test_retry_state_is_isolated_per_http_thread(self):
        barrier = threading.Barrier(2)
        seen = []
        client = ChatClient(Provider(api_key="offline"), on_usage=seen.append,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
                "choices":[{"message":{"content":"OK"},"finish_reason":"stop"}]})))
        def call(retry):
            client.retrying = retry
            barrier.wait(timeout=5)
            client.complete([])
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(call, (True, False)))
        finally: client.close()
        self.assertEqual(sorted(s["retry_requests"] for s in seen), [0, 1])

    def test_pdf_crops_are_rendered_on_owner_thread_and_keep_complete_figures(self):
        owner = threading.get_ident()
        rendered, request_threads = [], []
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"figures.pdf"
            with fitz.open() as doc:
                doc.new_page(width=300,height=300)
                doc.save(source)
            options = Options(vision_review=True, translation_workers=2)
            blocks = [Block(f"b{i}",0,[10,10,150,100],"Accuracy",unit_id=f"f{i}") for i in range(2)]
            units = [Unit(f"f{i}",0,"figure",[10,10,150,100],f"Figure {i}",[f"b{i}"]) for i in range(2)]
            project = Project(str(source),"",vars(options),blocks=blocks,units=units)
            real_open = fitz.open
            def open_on_owner(*args, **kwargs):
                rendered.append(threading.get_ident())
                return real_open(*args, **kwargs)
            def handle(request):
                request_threads.append(threading.get_ident())
                value = json.loads(request.content)["messages"][-1]["content"]
                self.assertEqual(value[1]["type"], "image_url")
                body = json.loads(value[0]["text"])
                self.assertEqual(body["unit"]["kind"], "complete_figure")
                return response(request)
            client = ChatClient(Provider(api_key="offline"), transport=httpx.MockTransport(handle))
            try:
                with patch("pymupdf.open", side_effect=open_on_owner):
                    translate_project(project, options, client, lambda: None, lambda _: None)
            finally: client.close()
        self.assertEqual(rendered, [owner, owner])
        self.assertTrue(all(t != owner for t in request_threads))

    def test_ipc_messages_are_serialized_across_threads(self):
        class Connection:
            active = 0
            peak = 0
            messages = []
            def send(self, message):
                self.active += 1
                self.peak = max(self.peak, self.active)
                time.sleep(0.001)
                self.messages.append(message)
                self.active -= 1
        connection = Connection()
        sender = _MessageSender(connection)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(sender.put, range(30)))
        self.assertEqual(connection.peak, 1)
        self.assertEqual(sorted(connection.messages), list(range(30)))

    def test_worker_count_bounds(self):
        for value in (0, 5, True, "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Options(translation_workers=value).validate()


if __name__ == "__main__":
    unittest.main()
