from __future__ import annotations
import copy
import http.server
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
import pymupdf as fitz

from papertranslator.api import APIError, ChatClient
from papertranslator.engine import Engine, cached_project_path, resume_project_path
from papertranslator.extraction import analyze
from papertranslator.layout import export_pdf
from papertranslator.locking import job_lock
from papertranslator.models import Block, Cancelled, Options, Project, Provider, atomic_json
from papertranslator.translation import translate_project
from papertranslator.worker import execute


def hold_job(source, ready, release):
    with job_lock(source):
        ready.set()
        release.wait(15)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'PAPERTRANSLATOR_DATA_DIR': str(self.root / 'settings')})
        env.start()
        self.addCleanup(env.stop)
        self.source, self.path = self.root / 'source.pdf', self.root / 'project.json'
        self.options = Options(ocr=False, translate_figures=False, vision_review=False)
        with fitz.open() as doc:
            for text in ('First scientific paragraph.', 'Second scientific paragraph.'):
                doc.new_page(width=400, height=300).insert_text((40, 65), text, fontsize=12)
            doc.save(self.source)

    def project(self, options=None):
        return analyze(str(self.source), options or self.options, str(self.path), threading.Event(), lambda _: None)

    def test_output_directory_does_not_change_job_identity(self):
        a = cached_project_path(str(self.source), str(self.root / 'a'))
        b = cached_project_path(str(self.source), str(self.root / 'b'))
        self.assertEqual(a, b)

    def test_opened_project_is_used_by_actual_worker(self):
        project = self.project()
        project.blocks[0].translation, project.blocks[0].status = '人工校对结果', 'edited'
        project.save(self.path)
        requested = []
        def complete(client, messages, **kwargs):
            segments = json.loads(messages[-1]['content'])['segments']
            requested.extend(s['source'] for s in segments)
            return json.dumps({'segments': [{'id': s['id'], 'translation': '新译文'} for s in segments]})
        output = self.root / 'different-output'
        events = queue.Queue()
        request = {'kind': 'queue', 'mode': 'translate', 'files': [str(self.source)], 'output': str(output),
                   'options': vars(self.options), 'provider': {'api_key': 'offline', 'vision': False},
                   'autoexport': False, 'project_paths': {str(self.source): str(self.path)}}
        with patch('papertranslator.api.ChatClient.complete', complete):
            execute(request, threading.Event(), events)
        saved = Project.load(cached_project_path(str(self.source), str(output)))
        self.assertEqual(saved.blocks[0].translation, '人工校对结果')
        self.assertEqual(requested, ['Second scientific paragraph.'])
        self.assertEqual(Project.load(self.path).blocks[0].translation, '人工校对结果')

    def test_legacy_cache_discovered_from_another_destination(self):
        project = self.project()
        project.blocks[0].translation, project.blocks[0].status = '已有译文', 'translated'
        old = self.root / 'settings' / 'jobs' / 'old-output-identity' / 'project.ptrans.json'
        project.save(old)
        chosen = resume_project_path(str(self.source), str(self.root / 'new'))
        self.assertEqual(Project.load(chosen).blocks[0].translation, '已有译文')
        self.assertTrue(old.exists())

    def test_wrong_explicit_project_rejected_before_copy(self):
        project = self.project()
        project.source = str(self.root / 'different.pdf')
        project.save(self.path)
        with self.assertRaisesRegex(ValueError, '不对应'):
            resume_project_path(str(self.source), str(self.root), str(self.path))

    def test_other_process_blocks_job_and_releases_after_termination(self):
        ctx = mp.get_context('spawn')
        ready, release = ctx.Event(), ctx.Event()
        process = ctx.Process(target=hold_job, args=(str(self.source), ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(10))
            with self.assertRaisesRegex(ValueError, '另一个程序窗口'):
                with job_lock(self.source): pass
            process.terminate()
            process.join(5)
            self.assertFalse(process.is_alive())
            with job_lock(self.source): pass
        finally:
            if process.is_alive(): process.terminate()
            process.join()
            process.close()

    def test_outdated_editor_cannot_overwrite_new_progress(self):
        self.project()
        old, new = Project.load(self.path), Project.load(self.path)
        new.blocks[0].translation = '另一窗口的新结果'
        new.save(self.path)
        old.blocks[0].translation = '旧窗口的内容'
        with self.assertRaisesRegex(ValueError, '重新载入'):
            old.save(self.path)
        self.assertEqual(Project.load(self.path).blocks[0].translation, '另一窗口的新结果')

    def test_atomic_saves_use_distinct_temporary_files(self):
        barrier = threading.Barrier(2)
        original, paths, errors = os.replace, [], []
        target = self.root / 'shared.json'
        def replace(source, destination):
            if str(source) not in paths:
                paths.append(str(source))
                barrier.wait(5)
            original(source, destination)
        def write(number):
            try: atomic_json(target, {'number': number})
            except Exception as e: errors.append(e)
        with patch('papertranslator.models.os.replace', replace):
            threads = [threading.Thread(target=write, args=(i,)) for i in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(8)
        self.assertFalse(errors, errors)
        self.assertEqual(len(set(paths)), 2)
        self.assertIn(json.loads(target.read_text())['number'], (0, 1))
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_source_update_creates_new_analysis_and_retains_old_project(self):
        project = self.project()
        project.blocks[0].translation, project.blocks[0].status = '旧版译文', 'edited'
        project.save(self.path)
        old_hash = project.source_hash
        with fitz.open(self.source) as doc:
            doc.set_metadata({'title': 'Updated document'})
            doc.saveIncr()
        fresh = self.project()
        self.assertNotEqual(fresh.source_hash, old_hash)
        self.assertFalse(any(b.translation for b in fresh.blocks))
        archived = list(self.root.glob('*before-source-change*.ptrans.json'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(Project.load(archived[0], verify=False).blocks[0].translation, '旧版译文')
        self.project()
        self.assertEqual(len(list(self.root.glob('*before-source-change*.ptrans.json'))), 1)

    def test_changed_export_range_keeps_unselected_page_original(self):
        project = self.project()
        for b in project.blocks: b.translation, b.status = '翻译内容', 'edited'
        self.options.page_range = '1'
        output = self.root / 'result.pdf'
        report = export_pdf(project, output, self.options)
        self.assertEqual([x['page'] for x in report['modifications']], [1])
        with fitz.open(output) as doc:
            self.assertIn('翻译内容', doc[0].get_text())
            self.assertIn('Second scientific paragraph', doc[1].get_text())

    def test_export_range_expansion_requires_analysis(self):
        self.options.page_range = '1'
        project = self.project()
        self.options.page_range = '1-2'
        with self.assertRaisesRegex(ValueError, '尚未分析'):
            export_pdf(project, self.root / 'result.pdf', self.options)
        self.assertFalse((self.root / 'result.pdf').exists())
        self.assertEqual(project.selected_pages, [0])

    def test_disabled_cached_figure_is_not_sent_or_exported_but_caption_is(self):
        project = self.project()
        label = project.blocks[0]
        label.role = 'figure_label'
        label.translation, label.status = '缓存的图内译文', 'translated'
        project.blocks[1].role = 'figure_caption'
        calls = []
        def handle(req):
            payload = json.loads(req.content)
            calls.append(payload)
            segments = json.loads(payload['messages'][-1]['content'])['segments']
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'segments': [
                {'id': s['id'], 'translation': '图题译文'} for s in segments]})}, 'finish_reason': 'stop'}]})
        client = ChatClient(Provider(api_key='offline', vision=False), transport=httpx.MockTransport(handle))
        try: translate_project(project, self.options, client, lambda: None, lambda _: None)
        finally: client.close()
        self.assertNotIn(label.text, json.dumps(calls))
        self.assertEqual(label.translation, '缓存的图内译文')
        report = export_pdf(project, self.root / 'output.pdf', self.options)
        self.assertNotIn(label.id, [x['id'] for x in report['modifications']])
        self.assertIn(project.blocks[1].id, [x['id'] for x in report['modifications']])

    def test_disabled_figures_filter_fullpage_ocr_results(self):
        with fitz.open() as image_doc:
            image_doc.new_page(width=300, height=150).insert_text((20, 45), 'Figure label', fontsize=16)
            png = image_doc[0].get_pixmap().tobytes('png')
        with fitz.open() as doc:
            page = doc.new_page(width=400, height=500)
            page.insert_image((40, 100, 340, 250), stream=png)
            doc.save(self.source)
        self.options.ocr = True
        labels = [Block('label', 0, [60, 130, 200, 150], 'Figure label', kind='raster'),
                  Block('body', 0, [40, 350, 300, 370], 'Scanned body paragraph.', kind='raster')]
        with patch('papertranslator.extraction.OCR.recognize', return_value=labels):
            project = self.project()
        self.assertEqual([b.text for b in project.blocks], ['Scanned body paragraph.'])


class NetworkTests(unittest.TestCase):
    def test_read_wait_honors_provider_setting_without_repeated_timeout_requests(self):
        calls, usage = [], []
        def stall(request):
            calls.append(request.extensions['timeout'])
            raise httpx.ReadTimeout('offline simulated stall')
        client = ChatClient(Provider(api_key='offline', timeout=180), transport=httpx.MockTransport(stall), on_usage=usage.append)
        try:
            with self.assertRaisesRegex(APIError, '未自动重发'): client.test()
        finally: client.close()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['read'], 180)
        self.assertEqual(len(usage), 1)

    def test_actual_socket_wait_is_cancellable_and_normal_request_still_works(self):
        started = threading.Event()
        release = threading.Event()
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if body['model'] == 'stall':
                    started.set()
                    release.wait(5)
                    return
                data = json.dumps({'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cancel, errors = threading.Event(), []
        provider = Provider(base_url=f'http://127.0.0.1:{server.server_port}', api_key='offline', model='stall', timeout=180)
        client = ChatClient(provider, cancel)
        def request():
            try: client.test()
            except Exception as e: errors.append(e)
        worker = threading.Thread(target=request)
        try:
            worker.start()
            self.assertTrue(started.wait(4))
            before = time.monotonic()
            cancel.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertLess(time.monotonic() - before, 2)
            self.assertIsInstance(errors[0], Cancelled)
            cancel.clear()
            provider.model = 'normal'
            self.assertEqual(client.test(), 'OK')
        finally:
            release.set()
            cancel.set()
            worker.join(6)
            client.close()
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
