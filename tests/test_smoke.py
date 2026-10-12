"""Run isolated checks: never use a real student or production database."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.request import urlopen
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.pop("DATABASE_URL", None)
os.environ.pop("REQUIRE_POSTGRES", None)
import server


class SmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        server.DATA_DIR = Path(cls.temp.name)
        server.DB = server.DATA_DIR / "smoke.sqlite3"
        server.DATABASE_URL = ""
        server.USE_POSTGRES = False
        server.init_db()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def test_health(self):
        with urlopen(self.base_url + "/api/health", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response), {"ok": True})

    def test_frontend_assets(self):
        for path, marker in (
            ("/", b"<!DOCTYPE html>"),
            ("/app.js", b"currentUser"),
            ("/style.css", b"{"),
        ):
            with self.subTest(path=path):
                with urlopen(self.base_url + path, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn(marker, response.read())


if __name__ == "__main__":
    unittest.main()
