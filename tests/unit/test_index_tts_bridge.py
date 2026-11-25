"""IndexTTS uses the configured reference and checks access before calling the model."""
from fastapi.testclient import TestClient

from integrations.open_llm_vtuber.agentstrata_vtuber.index_tts_bridge import create_app


def test_index_tts_synthesizes_only_the_configured_reference():
    calls = []
    voice_id = "a" * 64
    auth = "Bearer " + "token_" * 9
    audio = b"RIFF sample WAV"

    def synthesize(text):
        calls.append(text)
        return audio

    app = create_app(synthesize, voice_id=voice_id, auth=auth, port=9915)
    client = TestClient(app, base_url="http://127.0.0.1:9915")
    query = {"text": "今天想聊些什么？", "ref_audio_path": voice_id, "text_lang": "zh"}
    allowed = {"Authorization": auth}

    assert client.get("/tts", params=query).status_code == 403
    assert client.get("/tts", params=query, headers=allowed | {"Host": "example.test"}).status_code == 403
    assert client.get("/tts", params=query | {"ref_audio_path": "C:/reference.wav"}, headers=allowed).status_code == 404
    assert client.get("/tts", params=query | {"text_lang": "ja"}, headers=allowed).status_code == 400
    assert calls == []

    response = client.get("/tts", params=query, headers=allowed)
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == audio
    assert calls == [query["text"]]
