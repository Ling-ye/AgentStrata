"""Local IndexTTS playback over the companion's managed TTS transport."""
from __future__ import annotations

import argparse
import hashlib
import hmac
from io import BytesIO
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Callable
import wave

from fastapi import FastAPI, HTTPException, Request, Response


# IndexTeam/IndexTTS-1.5 at 25851a6036dfd3095bb70fb3c8f49217104672c3.
# Only official, frozen artifacts are passed to the upstream PyTorch loader.
MODEL_DIGESTS = {
    "gpt.pth": "44460b820a8afd58f68f3d3e69113e7900c8730bf519ecf158c081f2b8991240",
    "bigvgan_generator.pth": "a2458834d8277e76eb8614c9751b5e8eaa0474eab706f0ecfafcb600023133ed",
    "bpe.model": "b2a5ce8090d32da3642cc4f81fdc996376bc6dd3f4cd5e3d165f71120d9f2bc8",
    "config.yaml": "864280aeb82a722ce561078c7f7f16a63b0c6cb270e9b9566a8ce5bffef38954",
}


def create_app(synthesize: Callable[[str], bytes], *, voice_id: str, auth: str, port: int) -> FastAPI:
    if not re.fullmatch(r"Bearer [a-zA-Z0-9_-]{32,128}", auth):
        raise ValueError("Set AGENTSTRATA_INDEX_TTS_AUTH to a private Bearer credential")
    credential = auth.encode("ascii")
    lock = Lock()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health():
        return {"ok": True, "engine": "indextts-1.5"}

    @app.get("/tts")
    def tts(request: Request, text: str, ref_audio_path: str, text_lang: str = "auto"):
        if (request.headers.get("host") != f"127.0.0.1:{port}"
                or not hmac.compare_digest(request.headers.get("authorization", "").encode("utf-8"), credential)):
            raise HTTPException(status_code=403)
        if ref_audio_path != voice_id:
            raise HTTPException(status_code=404, detail="Voice is not available")
        if not text.strip() or len(text) > 12000 or text_lang not in {"auto", "zh", "en"}:
            raise HTTPException(status_code=400, detail="Expected Chinese or English dialogue text")
        # IndexTTS keeps the reference embedding and its inference buffers on the model.
        with lock:
            audio = synthesize(text)
        return Response(audio, media_type="audio/wav")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()
    auth = os.environ.get("AGENTSTRATA_INDEX_TTS_AUTH", "")
    if not re.fullmatch(r"Bearer [a-zA-Z0-9_-]{32,128}", auth):
        parser.error("Set AGENTSTRATA_INDEX_TTS_AUTH to a private Bearer credential")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be an explicit loopback port")

    for name, expected in MODEL_DIGESTS.items():
        digest = hashlib.sha256()
        with (args.model_dir / name).open("rb") as source:
            for block in iter(lambda: source.read(1024**2), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError(f"IndexTTS checkpoint digest differs for {name}")

    reference = args.reference.read_bytes()
    with wave.open(BytesIO(reference), "rb") as source:
        if not source.getnframes():
            raise ValueError("IndexTTS reference WAV is empty")
    voice_id = hashlib.sha256(reference).hexdigest()
    from indextts.infer import IndexTTS
    import uvicorn

    engine = IndexTTS(cfg_path=str(args.model_dir / "config.yaml"), model_dir=str(args.model_dir),
                      device=args.device, is_fp16=args.device.startswith("cuda"), use_cuda_kernel=False)
    with TemporaryDirectory(prefix="agentstrata-index-tts-") as temporary:
        root = Path(temporary)
        prompt = root / "reference.wav"
        prompt.write_bytes(reference)
        output = root / "reply.wav"

        def synthesize(text: str) -> bytes:
            output.unlink(missing_ok=True)
            engine.infer(str(prompt), text, str(output))
            audio = output.read_bytes()
            with wave.open(BytesIO(audio), "rb") as wav:
                if not wav.getnframes():
                    raise ValueError("IndexTTS did not produce audio")
            return audio

        app = create_app(synthesize, voice_id=voice_id, auth=auth, port=args.port)
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
