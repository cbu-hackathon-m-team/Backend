import functools
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from runner.compose import Compose
from runner.history import History
from runner.http import execute_http


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = functools.partial(
            QuietHandler, directory=str(Path(__file__).resolve().parents[1] / "fixtures/site")
        )
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_http_and_durable_history(self):
        result = execute_http(self.url)
        self.assertEqual(result.status, "PASS")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            History(path).save(result)
            self.assertEqual(History(path).latest()[0]["id"], result.id)

    def test_unexpected_status_is_failure(self):
        result = execute_http(self.url + "/missing")
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.details["status_code"], 404)

    def test_network_error_is_not_business_failure(self):
        with patch("runner.http.build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError()
            self.assertEqual(execute_http(self.url).status, "ERROR")

    def test_compose_requires_owned_project_and_keeps_volumes(self):
        path = Path(__file__).resolve().parents[1] / "infra/compose.yaml"
        with self.assertRaises(ValueError):
            Compose(path, "production")
        with patch("runner.compose.subprocess.run") as run:
            Compose(path, "runner-smoke").down()
            command = run.call_args.args[0]
            self.assertIn("runner-smoke", command)
            self.assertNotIn("--volumes", command)


if __name__ == "__main__":
    unittest.main()
