import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import make_config

from paywall import cloudflare


class FakeCloudflare(BaseHTTPRequestHandler):
    calls = []
    routes = {}

    def log_message(self, *args):
        pass

    def _reply(self, result, success=True):
        body = json.dumps({"success": success, "errors": [] if success else [{"message": "nope"}], "result": result})
        self.send_response(200 if success else 400)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.calls.append((self.command, self.path, self.headers.get("Content-Type"), body))
        if self.headers.get("Authorization") != "Bearer token":
            return self._reply(None, success=False)
        if self.path.startswith("/zones?name="):
            name = self.path.split("=", 1)[1]
            return self._reply([{"id": f"zone-{name}"}] if name in ("example.com", "example.org") else [])
        if self.path.endswith("/workers/routes") and self.command == "GET":
            zone = self.path.split("/")[2]
            return self._reply(self.routes.get(zone, []))
        return self._reply({"id": "ok"})

    do_GET = do_POST = do_PUT = do_DELETE = _handle


@pytest.fixture
def fake_api(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), FakeCloudflare)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(cloudflare, "API_BASE", f"http://127.0.0.1:{server.server_port}")
    FakeCloudflare.calls = []
    FakeCloudflare.routes = {
        "zone-example.com": [
            {"id": "r-stale", "pattern": "old.example.com/*", "script": "paywall-worker"},
            {"id": "r-other", "pattern": "blog.example.com/*", "script": "something-else"},
        ]
    }
    yield FakeCloudflare
    server.shutdown()


def test_deploy_uploads_worker_and_syncs_routes(tmp_path, fake_api):
    conf = make_config(tmp_path, cf_api_token="token", cf_account_id="acc")
    log = []
    cloudflare.deploy(conf, log=log.append)
    methods = [(c[0], c[1]) for c in fake_api.calls]
    assert ("PUT", "/accounts/acc/workers/scripts/paywall-worker") in methods
    upload = next(c for c in fake_api.calls if c[1].startswith("/accounts/"))
    assert upload[2].startswith("multipart/form-data; boundary=")
    assert b'"main_module": "worker.js"' in upload[3]
    assert b'"name": "PAYWALL_SECRET"' in upload[3] and b"secret_text" in upload[3]
    assert b"export default" in upload[3]
    # existing route switched, new zone route created, stale route removed
    assert ("PUT", "/zones/zone-example.com/workers/routes/r-other") in methods
    assert ("POST", "/zones/zone-example.org/workers/routes") in methods
    assert ("DELETE", "/zones/zone-example.com/workers/routes/r-stale") in methods
    assert "Deployed 'paywall-worker'" in cloudflare.read_status(conf)


def test_deploy_reports_api_errors(tmp_path, fake_api):
    conf = make_config(tmp_path, cf_api_token="bad", cf_account_id="acc")
    with pytest.raises(cloudflare.CloudflareError):
        cloudflare.deploy(conf, log=lambda _m: None)


def test_deploy_requires_settings(tmp_path):
    with pytest.raises(cloudflare.CloudflareError):
        cloudflare.deploy(make_config(tmp_path), log=lambda _m: None)
