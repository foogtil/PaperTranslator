"""Offline packaged checks for task ownership, export options and socket cancellation."""
import copy
import http.server
import json
import multiprocessing as mp
from pathlib import Path
import threading
import time

import httpx
import pymupdf as fitz

from .api import APIError, ChatClient
from .engine import cached_project_path, resume_project_path
from .extraction import analyze
from .layout import export_pdf
from .locking import job_lock
from .models import Cancelled, Options, Project, Provider
from .translation import translate_project


def _hold(source, ready):
    with job_lock(source):
        ready.set()
        time.sleep(15)


def run_checks(directory):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    source, path = root / 'source.pdf', root / 'job.json'
    with fitz.open() as doc:
        for text in ('First scientific paragraph.', 'Second scientific paragraph.'):
            doc.new_page(width=400, height=300).insert_text((40, 65), text, fontsize=12)
        doc.save(source)
    path.unlink(missing_ok=True)
    options = Options(ocr=False, translate_figures=False, vision_review=False)
    project = analyze(str(source), options, str(path), threading.Event(), lambda _: None)
    project.blocks[0].translation, project.blocks[0].status = '人工校对结果', 'edited'
    project.save(path)
    checks = {'resume_independent_of_output_directory': cached_project_path(str(source), 'a') == cached_project_path(str(source), 'b')}
    selected = resume_project_path(str(source), str(root / 'new'), str(path))
    resumed = Project.load(selected)
    calls = []
    def response(request):
        segments = json.loads(json.loads(request.content)['messages'][-1]['content'])['segments']
        calls.extend(s['source'] for s in segments)
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'segments': [
            {'id': s['id'], 'translation': '新的译文'} for s in segments]})}, 'finish_reason': 'stop'}]})
    client = ChatClient(Provider(api_key='offline', vision=False), transport=httpx.MockTransport(response))
    try: translate_project(resumed, options, client, lambda: resumed.save(selected), lambda _: None)
    finally: client.close()
    checks['opened_project_keeps_manual_work'] = resumed.blocks[0].translation == '人工校对结果' and calls == ['Second scientific paragraph.']
    stale = Project.load(selected)
    resumed.blocks[0].translation = '较新的校对内容'
    resumed.save(selected)
    try:
        stale.save(selected)
        checks['stale_editor_cannot_overwrite'] = False
    except ValueError:
        checks['stale_editor_cannot_overwrite'] = Project.load(selected).blocks[0].translation == '较新的校对内容'
    options.page_range = '1'
    report = export_pdf(resumed, root / 'page-one.pdf', options)
    with fitz.open(root / 'page-one.pdf') as doc:
        checks['export_uses_current_page_range'] = [m['page'] for m in report['modifications']] == [1] and 'Second scientific' in doc[1].get_text()
    resumed.blocks[0].role = 'figure_label'
    report = export_pdf(resumed, root / 'figure-off.pdf', options)
    checks['disabled_figure_translation_not_exported'] = not report['modifications']
    with fitz.open(source) as doc:
        doc.set_metadata({'title': 'Updated source'})
        doc.saveIncr()
    fresh = analyze(str(source), options, str(path), threading.Event(), lambda _: None)
    checks['updated_source_rebuilds_and_retains_old_work'] = (fresh.source_hash != project.source_hash and
        not any(b.translation for b in fresh.blocks) and bool(list(root.glob('*before-source-change*.ptrans.json'))))
    ctx = mp.get_context('spawn')
    ready = ctx.Event()
    child = ctx.Process(target=_hold, args=(str(source), ready), daemon=True)
    child.start()
    try:
        locked = False
        if ready.wait(8):
            try:
                with job_lock(source): pass
            except ValueError: locked = True
        child.terminate()
        child.join(5)
        with job_lock(source):
            checks['process_task_lock_and_crash_release'] = locked and not child.is_alive()
    finally:
        if child.is_alive(): child.terminate()
        child.join()
        child.close()
    checks.update(network_checks())
    return checks


def network_checks():
    started, release, cancel = threading.Event(), threading.Event(), threading.Event()
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
    provider = Provider(base_url=f'http://127.0.0.1:{server.server_port}', model='stall', api_key='offline')
    client = ChatClient(provider, cancel)
    errors = []
    def request():
        try: client.test()
        except Exception as error: errors.append(error)
    worker = threading.Thread(target=request, daemon=True)
    try:
        worker.start()
        received = started.wait(4)
        cancel.set()
        worker.join(2)
        cancelled = received and not worker.is_alive() and errors and isinstance(errors[0], Cancelled)
        release.set()
        cancel.clear()
        provider.model = 'normal'
        success = client.test() == 'OK'
        return {'cancellable_socket_wait_and_reconnect': bool(cancelled and success),
                'configured_read_timeout': client.http.timeout.read == 180}
    finally:
        release.set()
        cancel.set()
        worker.join(6)
        client.close()
        server.shutdown()
        server.server_close()
        thread.join()
