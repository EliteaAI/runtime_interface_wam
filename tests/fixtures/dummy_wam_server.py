#!/usr/bin/env python3
"""Scripted dummy WAM server for the runtime_interface_wam e2e rig.

Unlike tools/wam_shim.py (which blindly forwards to a real Ollama server),
this server never talks to a real backend -- every response is scripted
in-process, so it can drive scenarios a live LLM can't reliably reproduce:
a stream with no usage frame at all, or a 4xx/5xx after consuming the body.

Routes:
    POST {auth_endpoint}                        -> Set-Cookie-style text
    POST /deployments/{model}/chat/completions  -> scripted completion

Scenario selection (completions route only):
    - request body "stream" falsy          -> non-streamed JSON + usage block
    - request body "stream" truthy:
        - stream_options.include_usage     -> SSE stream, final frame carries usage
        - otherwise                        -> SSE stream, no usage frame at all
    - header X-Dummy-Error: "<status>"     -> that status after consuming the body,
                                               overriding the two behaviors above

Run on the host (stdlib only):
    python3 tests/fixtures/dummy_wam_server.py
Env:
    DUMMY_WAM_PORT   default 8098
    DUMMY_WAM_COOKIE default "wam_session=local; Path=/"

Or drive it from a test with start(), which binds an ephemeral port and records every
request it served so a test can assert what actually reached the wire.
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("DUMMY_WAM_PORT", "8098"))
COOKIE = os.environ.get("DUMMY_WAM_COOKIE", "wam_session=local; Path=/")

COMPLETION_ID = "chatcmpl-dummy"
MODEL_ECHO_FALLBACK = "dummy-model"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "dummy-wam"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if length:
            return self.rfile.read(int(length))
        return b""

    def _plain(self, status, text, content_type="text/plain; charset=utf-8"):
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _route(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.endswith("/auth"):
            return "auth", None
        if path == "/health":
            return "health", None
        if path.startswith("/deployments/"):
            parts = path[len("/deployments/"):].split("/", 1)
            if len(parts) == 2 and parts[0] and parts[1] == "chat/completions":
                return "completions", parts[0]
        return "unknown", None

    def _completion_payload(self, model):
        return {
            "id": COMPLETION_ID,
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "dummy response"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 7,
                "total_tokens": 19,
            },
        }

    def _sse_chunk(self, model, delta, finish_reason=None, usage=None):
        chunk = {
            "id": COMPLETION_ID,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage is not None:
            chunk["usage"] = usage
        return "data: %s\n\n" % json.dumps(chunk)

    def _write_chunk(self, text):
        body = text.encode("utf-8")
        self.wfile.write(("%x\r\n" % len(body)).encode("ascii"))
        self.wfile.write(body)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _stream_completion(self, model, with_usage):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        self._write_chunk(self._sse_chunk(model, {"role": "assistant", "content": ""}))
        self._write_chunk(self._sse_chunk(model, {"content": "dummy"}))
        self._write_chunk(self._sse_chunk(model, {"content": " response"}, finish_reason="stop"))
        if with_usage:
            self._write_chunk(self._sse_chunk(model, {}, usage={
                "prompt_tokens": 12,
                "completion_tokens": 7,
                "total_tokens": 19,
            }))
        self._write_chunk("data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")

    def _record(self, kind, payload=None):
        served = getattr(self.server, "served", None)
        if served is None:
            return
        served.append({
            "kind": kind,
            "path": self.path,
            "method": self.command,
            "headers": dict(self.headers),
            "payload": payload,
        })

    def _handle_completions(self, model):
        body = self._read_body()
        try:
            payload = json.loads(body) if body else {}
        except ValueError:
            payload = {}

        self._record("completions", payload)

        error_status = self.headers.get("X-Dummy-Error")
        if error_status:
            self._json(int(error_status), {
                "error": {"message": "scripted error", "type": "dummy_error"},
            })
            return

        model = payload.get("model") or model or MODEL_ECHO_FALLBACK
        stream = bool(payload.get("stream"))

        if not stream:
            self._json(200, self._completion_payload(model))
            return

        stream_options = payload.get("stream_options") or {}
        self._stream_completion(model, with_usage=bool(stream_options.get("include_usage")))

    def _handle(self):
        kind, model = self._route()
        if kind == "auth":
            self._read_body()
            self._record("auth")
            self._plain(200, COOKIE)
        elif kind == "health":
            self._record("health")
            self._plain(200, "ok")
        elif kind == "completions":
            self._handle_completions(model)
        else:
            self._read_body()
            self._record("unknown")
            self._plain(404, "dummy-wam: no route for %s" % self.path)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


class DummyWam:  # pylint: disable=R0903
    """A running dummy WAM, plus the log of what actually reached it."""

    def __init__(self, server):
        self._server = server
        self.served = server.served
        self.base_url = "http://127.0.0.1:%d" % server.server_address[1]
        self.auth_url = "%s/wam/auth" % self.base_url

    def completions(self):
        """Only the completion requests, in order."""
        return [entry for entry in self.served if entry["kind"] == "completions"]

    def clear(self):
        del self.served[:]

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


def start(port=0):
    """Serve in a daemon thread on `port` (0 = ephemeral) and return a DummyWam handle."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.served = []
    server.daemon_threads = True
    #
    threading.Thread(target=server.serve_forever, daemon=True).start()
    #
    return DummyWam(server)


if __name__ == "__main__":
    print("dummy-wam on 0.0.0.0:%d" % PORT, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
