"""Use the companion's private managed voice worker with the upstream TTS interface."""
from __future__ import annotations

import os
from pathlib import Path
import re
from urllib.parse import urlsplit


def install() -> None:
    endpoints = {}
    for provider in ("GPT_SOVITS", "INDEX_TTS"):
        url = os.environ.get(f"AGENTSTRATA_{provider}_URL")
        auth = os.environ.get(f"AGENTSTRATA_{provider}_AUTH")
        if not url or not auth:
            continue
        address = urlsplit(url)
        if (address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port
                or address.path != "/tts" or address.username or address.query
                or not re.fullmatch(r"Bearer [a-zA-Z0-9_-]{32,128}", auth)):
            raise ValueError("Invalid companion voice endpoint")
        if url in endpoints and endpoints[url] != auth:
            raise ValueError("Conflicting credentials for companion voice endpoint")
        endpoints[url] = auth
    if not endpoints:
        return
    import requests
    from open_llm_vtuber.tts.tts_factory import TTSFactory
    from open_llm_vtuber.tts.tts_interface import TTSInterface

    original = TTSFactory.get_tts_engine

    class VoiceEngine(TTSInterface):
        def __init__(self, url: str, identifier: str, language: str):
            if not re.fullmatch(r"[a-f0-9]{64}", identifier):
                raise ValueError("Playback needs a recorded character voice")
            self.url, self.identifier, self.language = url, identifier, language

        def generate_audio(self, text, file_name_no_ext=None):
            with requests.Session() as client:
                client.trust_env = False
                response = client.get(self.url, params={"text": text, "text_lang": self.language,
                    "ref_audio_path": self.identifier}, headers={"Authorization": endpoints[self.url]},
                    allow_redirects=False, timeout=120)
            response.raise_for_status()
            if not response.content.startswith(b"RIFF"):
                raise ValueError("Voice worker did not return a WAV")
            path = self.generate_cache_file_name(file_name_no_ext, "wav")
            Path(path).write_bytes(response.content)
            return path

    def factory(engine_type, **options):
        api_url = options.get("api_url")
        if engine_type == "gpt_sovits_tts" and api_url in endpoints:
            return VoiceEngine(api_url, options.get("ref_audio_path", ""), options.get("text_lang", "auto"))
        return original(engine_type, **options)

    TTSFactory.get_tts_engine = staticmethod(factory)
