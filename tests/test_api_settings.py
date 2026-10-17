import json
import threading
import unittest
from unittest.mock import Mock, patch

import httpx

from papertranslator.api import ChatClient, authentication_headers, validate_connection
from papertranslator.models import Options, Provider


class APISettingsTests(unittest.TestCase):
    def test_screenshot_error_is_blocked_before_transport_without_disclosing_key(self):
        key = 'offline-' + 'x' * 213 + '中文'
        called = []
        client = ChatClient(Provider(api_key=key), transport=httpx.MockTransport(lambda r: called.append(r)))
        try:
            with self.assertRaises(ValueError) as raised:
                client.test()
            message = str(raised.exception)
            self.assertIn('API Key', message)
            self.assertIn('222', message)
            self.assertNotIn(key, message)
            self.assertNotIn('offline-', message)
            self.assertFalse(called)
        finally:
            client.close()

    def test_fullwidth_and_invisible_key_characters_are_rejected(self):
        for suffix in ('：', '\u200b', '\ufeff', '\u00a0'):
            with self.subTest(character=ord(suffix)):
                with self.assertRaisesRegex(ValueError, 'API Key'):
                    authentication_headers(Provider(api_key='fake-only' + suffix))

    def test_ascii_control_and_embedded_whitespace_are_rejected(self):
        for key in ('fake only', 'fake\r\nInjected', 'fake\tkey', 'fake\x00key', 'fake\x7fkey'):
            with self.subTest(key=repr(key)):
                with self.assertRaisesRegex(ValueError, 'API Key'):
                    authentication_headers(Provider(api_key=key))

    def test_pasted_bearer_prefix_has_actionable_error(self):
        with self.assertRaisesRegex(ValueError, 'Bearer 前缀'):
            authentication_headers(Provider(api_key='Bearer fake-only'))

    def test_header_name_is_validated(self):
        for name in ('鉴权', 'Authorization:', 'x api key', 'x-api-key\r\n'):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, '请求头名称'):
                    authentication_headers(Provider(api_key='fake-only', auth_header=name))

    def test_ascii_padding_and_mimo_authentication_remain_supported(self):
        headers = authentication_headers(Provider(api_key=' \tfake-only\r\n'))
        self.assertEqual(headers['Authorization'], 'Bearer fake-only')
        headers = authentication_headers(Provider(api_key='fake-only', auth_header='api-key'))
        self.assertEqual(headers['api-key'], 'fake-only')
        self.assertNotIn('Authorization', headers)
        self.assertEqual(authentication_headers(Provider(), require_key=False), {'Content-Type': 'application/json'})

    def test_chinese_text_and_model_names_still_use_utf8_json(self):
        def handle(request):
            body = json.loads(request.content)
            self.assertEqual(body['messages'][0]['content'], '将这段数学正文翻译为中文。')
            self.assertEqual(body['model'], '本地测试模型')
            return httpx.Response(200, json={'choices': [{'message': {'content': '中文译文'}, 'finish_reason': 'stop'}]})
        client = ChatClient(Provider(api_key='fake-only', model='本地测试模型'), transport=httpx.MockTransport(handle))
        try:
            self.assertEqual(client.complete([{'role': 'user', 'content': '将这段数学正文翻译为中文。'}]), '中文译文')
        finally:
            client.close()

    def test_gui_rejects_bad_credentials_before_analysis_and_task_launch(self):
        from papertranslator.gui import App
        app = Mock()
        app.busy = False
        app.files = ['offline.pdf']
        app._options.return_value = Options(ocr=False)
        app._capture_profile.return_value = Provider(api_key='fake-only中文')
        with patch('papertranslator.gui.messagebox.showerror') as dialog:
            App._run(app, 'translate')
        self.assertTrue(dialog.called)
        app._launch.assert_not_called()
        app._save_all.assert_not_called()
        app.notebook.select.assert_called_once_with(app.settings_tab)


if __name__ == '__main__':
    unittest.main()
