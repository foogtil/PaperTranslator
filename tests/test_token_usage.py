from __future__ import annotations
import json
import threading
import unittest
from unittest.mock import Mock
import httpx
from papertranslator.api import APIError, ChatClient
from papertranslator.models import Block, Cancelled, Options, Project, Provider, Unit
from papertranslator.prompt_context import TranslationContext
from papertranslator.translation import protect, restore, segment_results, translate_project, translation_fingerprint
from papertranslator.usage import usage_delta, format_usage


class ContextTests(unittest.TestCase):
    def test_requested_paragraph_is_complete_and_not_repeated_in_context(self):
        text = "Complete scientific paragraph. " * 300
        block = Block("a", 0, [0, 0, 100, 100], text)
        project = Project("offline", "", {}, blocks=[block])
        client = Mock(provider=Provider(), cancel=threading.Event())
        def complete(messages):
            payload = json.loads(messages[-1]["content"])
            self.assertEqual(payload["segments"][0]["source"], text)
            self.assertEqual(payload["document_context"], "")
            self.assertEqual(payload["page_context"], "")
            return '{"segments":[{"id":"a","translation":"完整译文"}]}'
        client.complete.side_effect = complete
        translate_project(project, Options(), client, lambda: None, lambda _: None)
        self.assertEqual(block.translation, "完整译文")

    def test_supporting_context_is_bounded_and_neighbours_retained(self):
        intro = Block("intro", 0, [0, 0, 100, 100], "Introduction " * 1000)
        before = Block("before", 3, [0, 0, 100, 100], "Previous paragraph definition.")
        body = Block("body", 3, [0, 0, 100, 100], "Current paragraph.")
        after = Block("after", 3, [0, 0, 100, 100], "Next paragraph explanation.")
        project = Project("offline", "", {}, blocks=[intro, before, body, after])
        data = TranslationContext(project).fields([body], None)
        self.assertLessEqual(len(data["document_context"]), 2400)
        self.assertLessEqual(len(data["page_context"]), 2000)
        self.assertIn(before.text, data["page_context"])
        self.assertIn(after.text, data["page_context"])
        self.assertNotIn(body.text, json.dumps(data))

    def test_full_context_option_restores_previous_scope(self):
        blocks = [Block(str(i), 0, [0,0,100,100], str(i) * 5000) for i in range(4)]
        project = Project("offline", "", {}, blocks=blocks)
        data = TranslationContext(project).fields([blocks[1]], None, compact=False)
        self.assertEqual(data["document_context"], "\n".join(b.text for b in blocks)[:9000])
        self.assertEqual(data["page_context"], "\n".join(b.text for b in blocks)[:12000])

    def test_whole_figure_has_all_ids_without_duplicate_requested_sources(self):
        blocks = [Block(str(i), 2, [0,0,100,100], f"Metric label {i}", unit_id="f") for i in range(30)]
        unit = Unit("f", 2, "figure", [0,0,300,300], "Fig. 1", [b.id for b in blocks])
        project = Project("offline", "", {}, blocks=blocks, units=[unit])
        fields = TranslationContext(project).fields(blocks, unit)
        self.assertEqual(fields["unit"]["labels_and_caption"], [{"id": b.id} for b in blocks])
        self.assertEqual(fields["page_context"], "")
        blocks[0].translation, blocks[0].status = "标签零", "translated"
        retry = TranslationContext(project).fields([blocks[1]], unit)
        labels = retry["unit"]["labels_and_caption"]
        self.assertEqual(len(labels), 30)
        self.assertEqual(labels[0]["source"], blocks[0].text)
        self.assertEqual(labels[0]["translation"], "标签零")
        self.assertEqual(labels[1], {"id": "1"})

    def test_context_option_does_not_invalidate_paid_translations(self):
        options = Options()
        original = translation_fingerprint(options, Provider().public_dict())
        options.compact_context = False
        self.assertEqual(original, translation_fingerprint(options, Provider().public_dict()))
        block = Block("a", 0, [0,0,100,100], "Source", translation="译文", status="translated")
        project = Project("offline", "", vars(Options()), blocks=[block], provider=Provider().public_dict())
        client = Mock(provider=Provider(), cancel=threading.Event())
        translate_project(project, options, client, lambda: None, lambda _: None)
        client.complete.assert_not_called()
        self.assertEqual(block.translation, "译文")

    def test_unrequested_entries_do_not_discard_valid_paid_results(self):
        values, errors = segment_results(json.dumps({"segments": [None, {"id": "extra", "translation": "x"},
            {"id": "a", "translation": "正确"}, {"id": "b", "translation": ""}]}), ["a", "b"])
        self.assertEqual(set(values), {"a"})
        self.assertEqual(set(errors), {"b"})

    def test_layout_overflow_reuses_translation_but_model_change_invalidates_it(self):
        options = Options(vision_review=False)
        block = Block("a", 0, [0,0,100,100], "Scientific paragraph", translation="完整译文", status="overflow")
        project = Project("offline", "", vars(options), blocks=[block], provider=Provider().public_dict())
        client = Mock(provider=Provider(), cancel=threading.Event())
        translate_project(project, options, client, lambda: None, lambda _: None)
        client.complete.assert_not_called()
        self.assertEqual(block.translation, "完整译文")
        client.provider.model = "changed-model"
        client.complete.return_value = '{"segments":[{"id":"a","translation":"新译文"}]}'
        translate_project(project, options, client, lambda: None, lambda _: None)
        client.complete.assert_called_once()
        self.assertEqual(block.translation, "新译文")

    def test_glossary_and_protected_values_remain_in_request(self):
        original = "Use $x^2$ at 95% accuracy [1]."
        block = Block("a", 0, [0,0,100,100], original)
        options = Options(glossary={"accuracy": "准确率", "matrix": "矩阵"})
        project = Project("offline", "", {}, blocks=[block])
        client = Mock(provider=Provider(), cancel=threading.Event())
        def complete(messages):
            self.assertIn("准确率", messages[0]["content"])
            self.assertIn("矩阵", messages[0]["content"])
            source = json.loads(messages[-1]["content"])["segments"][0]["source"]
            self.assertEqual(restore(source, protect(original)[1]), original)
            return json.dumps({"segments": [{"id":"a", "translation": source}]})
        client.complete.side_effect = complete
        translate_project(project, options, client, lambda: None, lambda _: None)
        self.assertEqual(block.translation, original)


class UsageTests(unittest.TestCase):
    def test_reasoning_and_cache_are_subsets_not_added_to_total(self):
        usage = usage_delta({"prompt_tokens": 100, "completion_tokens": 80,
            "prompt_cache_hit_tokens": 50, "completion_tokens_details": {"reasoning_tokens": 30}})
        self.assertEqual(usage["input_tokens"] + usage["output_tokens"], 180)
        self.assertEqual(usage["reasoning_tokens"], 30)
        self.assertEqual(usage["cached_input_tokens"], 50)
        self.assertEqual(usage["usage_missing_requests"], 0)

    def test_missing_usage_is_unknown_and_malformed_counts_are_tolerated(self):
        usage = usage_delta({"prompt_tokens": -2, "completion_tokens": "unknown", "prompt_tokens_details": []})
        self.assertEqual(usage["usage_missing_requests"], 1)
        self.assertIn("?", format_usage(usage))
        self.assertEqual(usage_delta(None)["requests"], 1)
        self.assertEqual(usage_delta({"prompt_tokens": 0, "completion_tokens": 0})["usage_missing_requests"], 0)

    def test_final_stream_usage_only_is_counted_once(self):
        seen, requests = [], []
        def handle(request):
            requests.append(json.loads(request.content))
            events = [{"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]},
                      {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 10,
                        "prompt_tokens_details": {"cached_tokens": 8}}}]
            return httpx.Response(200, headers={"content-type":"text/event-stream"},
                text="".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n")
        with_client = ChatClient(Provider(api_key="offline"), on_usage=seen.append, transport=httpx.MockTransport(handle))
        try:
            self.assertEqual(with_client.test(), "OK")
        finally:
            with_client.close()
        self.assertEqual(requests[0]["stream_options"], {"include_usage": True})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["connection_input_tokens"], 20)
        self.assertEqual(seen[0]["cached_input_tokens"], 8)

    def test_network_retry_counts_all_attempts_and_missing_usage(self):
        seen, calls = [], []
        def handle(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(503, headers={"retry-after":"0"})
            return httpx.Response(200, json={"choices":[{"message":{"content":"OK"},"finish_reason":"stop"}],
                "usage":{"prompt_tokens": 12, "completion_tokens": 3}})
        client = ChatClient(Provider(api_key="offline", retries=1), on_usage=seen.append, transport=httpx.MockTransport(handle))
        try: self.assertEqual(client.test(), "OK")
        finally: client.close()
        self.assertEqual(sum(s["requests"] for s in seen), 2)
        self.assertEqual(sum(s["retry_requests"] for s in seen), 1)
        self.assertEqual(sum(s["usage_missing_requests"] for s in seen), 1)
        self.assertEqual(sum(s["input_tokens"] for s in seen), 12)

    def test_failed_response_still_counts_reported_tokens(self):
        seen = []
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "choices":[{"message":{"content":""},"finish_reason":"length"}],
            "usage":{"prompt_tokens": 20, "completion_tokens": 100}}))
        client = ChatClient(Provider(api_key="offline"), on_usage=seen.append, transport=transport)
        client.retrying = True
        try:
            with self.assertRaises(APIError): client.complete([])
        finally: client.close()
        self.assertEqual(seen[0]["output_tokens"], 100)
        self.assertEqual(seen[0]["retry_requests"], 1)

    def test_cancelled_request_is_counted_without_inventing_tokens(self):
        cancel, seen = threading.Event(), []
        def handle(request):
            cancel.set()
            return httpx.Response(200, headers={"content-type":"text/event-stream"}, text='data: {"choices":[]}\n\n')
        client = ChatClient(Provider(api_key="offline"), cancel=cancel, on_usage=seen.append, transport=httpx.MockTransport(handle))
        try:
            with self.assertRaises(Cancelled): client.test()
        finally: client.close()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["usage_missing_requests"], 1)

    def test_custom_endpoint_does_not_get_unsupported_stream_options(self):
        def handle(request):
            self.assertNotIn("stream_options", json.loads(request.content))
            return httpx.Response(200, json={"choices":[{"message":{"content":"OK"},"finish_reason":"stop"}]})
        client = ChatClient(Provider(base_url="http://localhost/v1", api_key="offline"), transport=httpx.MockTransport(handle))
        try: self.assertEqual(client.test(), "OK")
        finally: client.close()

    def test_mimo_preserves_documented_payload_and_reads_usage(self):
        seen = []
        def handle(request):
            self.assertNotIn("stream_options", json.loads(request.content))
            return httpx.Response(200, json={"choices":[{"message":{"content":"OK"},"finish_reason":"stop"}],
                "usage":{"prompt_tokens": 12, "completion_tokens": 3, "prompt_tokens_details":{"cached_tokens": 5}}})
        client = ChatClient(Provider(base_url="https://api.xiaomimimo.com/v1", api_key="offline"),
                            on_usage=seen.append, transport=httpx.MockTransport(handle))
        try: self.assertEqual(client.test(), "OK")
        finally: client.close()
        self.assertEqual(seen[0]["cached_input_tokens"], 5)

    def test_visual_usage_is_separate_from_text(self):
        seen = []
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "choices":[{"message":{"content":"{}"},"finish_reason":"stop"}],
            "usage":{"prompt_tokens": 45, "completion_tokens": 8}}))
        client = ChatClient(Provider(api_key="offline"), on_usage=seen.append, transport=transport)
        try: client.read_crop(b"offline-image", "label")
        finally: client.close()
        self.assertEqual(seen[0]["vision_input_tokens"], 45)
        self.assertNotIn("translation_input_tokens", seen[0])


if __name__ == "__main__":
    unittest.main()
