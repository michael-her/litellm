from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from antigravity_cli import kill_tree
from verify_live import ROOT, curl, env_key
from production_lifecycle import with_production_restore


class FixtureServer(ThreadingHTTPServer):
    fail = False
    seen: tuple[str, ...] = ()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        assert isinstance(self.server, FixtureServer)
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen = (*self.server.seen, request["model"])
        if request.get("stream") and not self.server.fail:
            chunks = (
                {"id": "chatcmpl-vertex-fixture", "object": "chat.completion.chunk", "created": 1,
                 "model": "vertex-fixture/" + request["model"],
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}}]}
                for text in ("VERTEX_", "FIXTURE_OK")
            )
            finish = {"id": "chatcmpl-vertex-fixture", "object": "chat.completion.chunk", "created": 1,
                      "model": "vertex-fixture/" + request["model"],
                      "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
            encoded = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in (*chunks, finish))
                       + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        body = {"error": {"message": "vertex-fixture-quota", "type": "rate_limit_error", "code": "429"}} if (
            self.server.fail) else {
                "id": "chatcmpl-vertex-fixture", "object": "chat.completion", "created": 1,
                "model": "vertex-fixture/" + request["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "VERTEX_FIXTURE_OK"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }
        encoded = json.dumps(body).encode()
        self.send_response(429 if self.server.fail else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        pass


def stream_events(label: str) -> tuple[dict[str, object], ...]:
    body = (ROOT / f"logs/{label}.body").read_text(encoding="utf-8")
    return tuple(json.loads(line[6:]) for line in body.splitlines()
                 if line.startswith("data: ") and line != "data: [DONE]")


def main(port: int = 14606, prefix: str = "fixture") -> None:
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"Port {port} must be free before this isolated fixture test; stop only Antigravity first")
    server = FixtureServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    request = partial(curl, proxy_port=port, timeout_s=20)
    config = yaml.safe_load((ROOT / "config.generated.yaml").read_text())
    for entry in config["model_list"]:
        if entry["model_name"].startswith("vertex-fallback/"):
            entry["litellm_params"]["api_base"] = f"http://127.0.0.1:{server.server_port}/v1"
    config_path = ROOT / f"config.fixture-{port}.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    env = {
        **os.environ, "PYTHONPATH": str(ROOT) + os.pathsep + str(ROOT.parent),
        "PYTHONIOENCODING": "utf-8",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "ANTIGRAVITY_CLI": sys.executable,
        "ANTIGRAVITY_CLI_ARGS": json.dumps([str(ROOT / "tests/fake_agy.py"), "fallback-probe"]),
        "ANTIGRAVITY_PROXY_KEY": env_key(ROOT / "antigravity.local.env", "ANTIGRAVITY_PROXY_KEY"),
        "VERTEX_PROXY_KEY": env_key(ROOT.parent / ".vertex/vertex.local.env", "VERTEX_PROXY_KEY"),
    }
    with (ROOT / f"logs/{prefix}-server.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, "-m", "litellm.proxy.proxy_cli", "--config", str(config_path),
                                    "--host", "127.0.0.1", "--port", str(port)],
                                   env=env, cwd=ROOT.parent, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        for _ in range(150):
            try:
                ready = httpx.get(f"http://127.0.0.1:{port}/v1/models", headers={
                    "Authorization": "Bearer " + env["ANTIGRAVITY_PROXY_KEY"]}, timeout=1)
                if ready.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert process.poll() is None, "Fixture proxy exited during startup"
            time.sleep(0.2)
        else:
            raise RuntimeError("Fixture proxy did not start")
        response = request(prefix + "-fallback-success", "gemini-4-argon-low", expected_status=200)
        assert response["choices"][0]["message"]["content"] == "VERTEX_FIXTURE_OK"
        assert server.seen == ("gemini-4-argon-low",)
        print("Confirmed fallback-only upstream received exact original model; one request", flush=True)
        label = prefix + "-stream-success"
        request(label, "gemini-4-argon-low", stream=True, expected_status=200)
        events = stream_events(label)
        assert not any("error" in event for event in events), "A: in-stream error instead of fallback"
        assert "".join(choice.get("delta", {}).get("content", "") for event in events
                       for choice in event.get("choices", [])) == "VERTEX_FIXTURE_OK", "A: missing fallback text"
        assert "data: [DONE]" in (ROOT / f"logs/{label}.body").read_text()
        assert server.seen == ("gemini-4-argon-low", "gemini-4-argon-low")
        print("A: HTTP 200 fallback stream VERTEX_FIXTURE_OK, DONE, no error", flush=True)
        server.fail = True
        response = request(prefix + "-both-fail", "gemini-4-argon-low", expected_status=429)
        assert "agy-test-quota" in response["error"]["message"]
        assert "vertex-fixture-quota" in response["error"]["message"]
        print("Both-fail response preserved agy and Vertex fixture errors", flush=True)
        label = prefix + "-stream-both-fail"
        request(label, "gemini-4-argon-low", stream=True, expected_status=429)
        body = (ROOT / f"logs/{label}.body").read_text(encoding="utf-8")
        assert "agy-test-quota" in body
        print("B: HTTP 429 before stream; agy error visible", flush=True)
        label = prefix + "-stream-late-fail"
        request(label, "gemini-4-argon-medium", stream=True, expected_status=200)
        body = (ROOT / f"logs/{label}.body").read_text(encoding="utf-8")
        assert "AGY_FIRST_TOKEN" in body and "agy-late-quota" in body
        assert "VERTEX_FIXTURE_OK" not in body
        assert any("error" in event for event in stream_events(label))
        print("Late failure: HTTP 200, AGY_FIRST_TOKEN then agy error; stream closed", flush=True)
    finally:
        kill_tree(process.pid)
        process.wait()
        server.shutdown()
        server.server_close()
        worker.join()
        config_path.unlink(missing_ok=True)


if __name__ == "__main__":
    with_production_restore(main, 14606)
