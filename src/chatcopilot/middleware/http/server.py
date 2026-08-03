"""Stdlib HTTP server for machine-facing AgentStrata APIs."""
from __future__ import annotations

import argparse
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import urlsplit

from chatcopilot.middleware.http.auth import HttpError
from chatcopilot.middleware.http.routes import handle_request
from chatcopilot.project import ENV_PREFIX


class ChatCopilotHttpHandler(BaseHTTPRequestHandler):
    server_version = "AgentStrataHttp/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = urlsplit(self.path).path
        try:
            payload = self._read_json_body() if method in {"POST", "PUT", "PATCH"} else {}
            status, body = handle_request(method, path, self.headers, payload)
            self._send_json(status, body)
        except HttpError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.message})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - API boundary must not crash server.
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def _read_json_body(self) -> Dict[str, Any]:
        content_length = int(self.headers.get("Content-Length") or "0")
        if content_length <= 0:
            return {}
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise ValueError("仅支持 application/json")
        raw = self.rfile.read(content_length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 请求体格式错误: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON 请求体必须是对象")
        return payload

    def _send_json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    server = ThreadingHTTPServer((host, port), ChatCopilotHttpHandler)
    print(f"AgentStrata HTTP API listening on http://{host}:{port}")
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m chatcopilot http-api-server")
    parser.add_argument(
        "--host",
        default=os.environ.get(f"{ENV_PREFIX}_HTTP_API_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get(f"{ENV_PREFIX}_HTTP_API_PORT", "8787")),
    )
    args = parser.parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
