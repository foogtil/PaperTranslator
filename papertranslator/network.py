"""Synchronous HTTPX transport with cancellable asynchronous socket waits."""
import asyncio
import threading

import httpx

from .models import Cancelled


class _Stream(httpx.SyncByteStream):
    def __init__(self, owner, response):
        self.owner, self.response = owner, response

    def __iter__(self):
        iterator = self.response.stream.__aiter__()
        while True:
            try:
                yield self.owner.run(anext(iterator))
            except StopAsyncIteration:
                break

    def close(self):
        self.owner.run(self.response.aclose(), cancellable=False)


class CancellableTransport(httpx.BaseTransport):
    def __init__(self, cancelled, **kwargs):
        self.cancelled = cancelled
        self.kwargs = kwargs
        self.loop = None
        self.guard = threading.Lock()
        self.closed = False

    def start(self):
        with self.guard:
            if self.loop is None:
                self.transport = httpx.AsyncHTTPTransport(**self.kwargs)
                self.loop = asyncio.new_event_loop()
                self.thread = threading.Thread(target=self.loop.run_forever, name='api-sockets', daemon=True)
                self.thread.start()

    async def guarded(self, coroutine, cancellable):
        task = asyncio.create_task(coroutine)
        try:
            while not task.done():
                if cancellable and self.cancelled():
                    raise Cancelled('请求已取消。')
                await asyncio.wait({task}, timeout=0.1)
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def run(self, coroutine, cancellable=True):
        self.start()
        return asyncio.run_coroutine_threadsafe(self.guarded(coroutine, cancellable), self.loop).result()

    def handle_request(self, request):
        self.start()
        incoming = httpx.Request(request.method, request.url, headers=request.headers,
                                 content=request.read(), extensions=request.extensions)
        response = self.run(self.transport.handle_async_request(incoming))
        return httpx.Response(response.status_code, headers=response.headers,
                              stream=_Stream(self, response), extensions=response.extensions)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.loop is None:
            return
        try:
            self.run(self.transport.aclose(), cancellable=False)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join()
            self.loop.close()
