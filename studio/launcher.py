"""Verify the running workbench before exposing a launcher URL."""
import hashlib
import hmac
from http.client import HTTPException as HTTPProtocolError
import json
import os
import re
import secrets
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from fastapi import HTTPException

READINESS_PATH = "/api/launcher/ready"


def _proof(token, challenge, pid):
    value = f"AgentStrata Studio readiness\n{pid}\n{challenge}"
    return hmac.new(token.encode(), value.encode(), hashlib.sha256).hexdigest()


def register_readiness(app, token):
    @app.get(READINESS_PATH)
    def ready(challenge: str = ""):
        if not re.fullmatch(r"[0-9a-f]{64}", challenge):
            raise HTTPException(400, "Invalid readiness challenge")
        pid = os.getpid()
        return {"pid": pid, "proof": _proof(token, challenge, pid)}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def ready_url(home, *, expected_pid=None):
    """Probe only loopback; never send the launch token or follow a redirect."""
    try:
        launch = json.loads((home / "launcher.json").read_text(encoding="utf-8"))
        pid, url = launch["pid"], launch["url"]
        if type(pid) is not int or pid <= 0:
            raise ValueError
        if expected_pid is not None and (type(expected_pid) is not int or expected_pid <= 0 or pid != expected_pid):
            raise ValueError
        match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/#login=([A-Za-z0-9_-]{16,128})", url)
        if not match or int(match[1]) > 65535:
            raise ValueError
        challenge = secrets.token_hex(32)
        endpoint = url.split("/#", 1)[0] + READINESS_PATH + "?challenge=" + challenge
        # A workstation proxy must not observe even the readiness request.
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(endpoint, timeout=1) as response:
            body = response.read(1025)
            if response.status != 200 or len(body) > 1024:
                raise ValueError
        result = json.loads(body)
        if type(result["pid"]) is not int or result["pid"] != pid:
            raise ValueError
        if not isinstance(result["proof"], str) or not hmac.compare_digest(result["proof"], _proof(match[2], challenge, pid)):
            raise ValueError
        return url
    except (OSError, URLError, HTTPProtocolError, ValueError, TypeError, KeyError):
        raise ValueError("Local Studio is not ready or does not match its launcher record") from None
