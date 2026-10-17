import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from papertranslator.models import atomic_json


@unittest.skipUnless(os.name == "nt", "Windows file sharing behaviour")
class AtomicCacheTests(unittest.TestCase):
    def test_brief_reader_lock_is_retried_without_losing_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "project.json"
            path.write_text('{"status":"old"}', encoding="utf-8")
            error = PermissionError("simulated sharing conflict")
            error.winerror = 32
            replace = os.replace
            attempts = []
            def unlocked_replace(source, target):
                attempts.append(1)
                if len(attempts) == 1:
                    raise error
                replace(source, target)
            with patch("papertranslator.models.os.replace", side_effect=unlocked_replace) as call, \
                    patch("papertranslator.models.time.sleep") as sleep:
                atomic_json(path, {"status": "saved"})
            self.assertEqual(call.call_count, 2)
            sleep.assert_called_once_with(0.01)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"status": "saved"})

    def test_persistent_lock_has_bounded_retries_and_keeps_old_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "project.json"
            original = '{"status":"old"}'
            path.write_text(original, encoding="utf-8")
            error = PermissionError("simulated access conflict")
            error.winerror = 5
            with patch("papertranslator.models.os.replace", side_effect=error) as call, \
                    patch("papertranslator.models.time.sleep"), self.assertRaises(PermissionError):
                atomic_json(path, {"status": "new"})
            self.assertEqual(call.call_count, 8)
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
