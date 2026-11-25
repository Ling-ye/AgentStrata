"""Private process entry points for the pinned Python 3.10 GPT-SoVITS environment."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import hmac
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from threading import Lock
import time
import wave

PIN = "d7c2210da8c013e81a94bfc7b811a477c99fd506"
BASE = Path("GPT_SoVITS/pretrained_models")
PRETRAINED = BASE / "gsv-v2final-pretrained"
GPT = PRETRAINED / "s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt"
SOVITS = PRETRAINED / "s2G2333k.pth"
UVR = Path("tools/uvr5/uvr5_weights/HP5_only_main_vocal.pth")
SAMPLE_LINES = ("你好，今天很高兴见到你。我们聊聊刚完成的角色吧。",
                "窗外的灯火亮了，我会把这段故事慢慢讲给你听。")


class NeedSource(ValueError):
    pass


class OutOfMemory(RuntimeError):
    pass


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, encoding="utf-8", mode="w") as file:
        name = Path(file.name)
        json.dump(value, file, ensure_ascii=False, sort_keys=True)
    try:
        os.replace(name, path)
    finally:
        name.unlink(missing_ok=True)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def enter(root: Path) -> None:
    os.chdir(root)
    for folder in ("", "GPT_SoVITS", "tools/uvr5"):
        sys.path.insert(0, str(root / folder))
    os.environ.update(version="v2", PYTHONUTF8="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      NLTK_DATA=str(root / "nltk_data"))


def stage(job: Path, name: str) -> None:
    write(job, read(job) | {"stage": name, "status": "running"})


def convert(ffmpeg: Path, source: Path, output: Path, *, stereo=False) -> None:
    line = [str(ffmpeg), "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
            "-vn", "-ac", "2" if stereo else "1", "-ar", "44100" if stereo else "16000",
            "-c:a", "pcm_s16le", str(output)]
    if subprocess.run(line, stdin=subprocess.DEVNULL, check=False).returncode:
        raise NeedSource("无法解码语音源")


def recognizer(config: dict, language: str):
    import sherpa_onnx

    directory = Path(config["sensevoice"])
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(directory / "model.int8.onnx"), tokens=str(directory / "tokens.txt"),
        language=language, use_itn=True, num_threads=2,
    )


def separate(ffmpeg: Path, source: Path, work: Path) -> Path:
    from tools.uvr5.vr import AudioPre

    destination = work / "vocals"
    destination.mkdir()
    audio = work / "song.wav"
    convert(ffmpeg, source, audio, stereo=True)
    backend = AudioPre(10, str(Path.cwd() / UVR), "cpu", False)
    backend._path_audio_(str(audio), vocal_root=str(destination), format="wav")
    stems = list(destination.glob("*.wav"))
    if len(stems) != 1 or not stems[0].stat().st_size:
        raise NeedSource("歌曲没有分离出可用的主唱")
    return stems[0]


def _clips(samples):
    import numpy as np

    # Prefer low-energy boundaries near the target duration without cutting long silence.
    rate = 16000
    width = int(0.2 * rate)
    offset = 0
    while len(samples) - offset >= 3.1 * rate:
        nominal = min(len(samples), offset + 7 * rate)
        if len(samples) - nominal >= 3.1 * rate:
            stop = min(nominal + rate, len(samples))
            end = min(range(nominal - rate, stop + 1, width), key=lambda edge:
                      np.mean(np.abs(samples[edge - width:edge])))
        else:
            end = min(len(samples), offset + 9 * rate)
        clip = samples[offset:end]
        offset = end
        if np.sqrt(np.mean(clip ** 2)) > .004:
            yield clip


def build_dataset(config: dict, sources: list[dict], work: Path) -> dict:
    import numpy as np

    ffmpeg = Path(config["ffmpeg"])
    processed = work / "preprocessed"
    processed.mkdir(exist_ok=True)
    clips = work / "clips"
    clips.mkdir(exist_ok=True)
    records = []
    language = sources[0]["language"]
    asr = recognizer(config, language)
    for index, source in enumerate(sources):
        area = processed / str(index)
        area.mkdir(exist_ok=True)
        audio = work / source["file"]
        if digest(audio) != source["sha256"]:
            raise NeedSource("音源与已记录的摘要不一致")
        if source["kind"] == "song":
            audio = separate(ffmpeg, audio, area)
        mono = area / "mono.wav"
        convert(ffmpeg, audio, mono)
        with wave.open(str(mono), "rb") as stream:
            pcm = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype("float32") / 32768
        for clip in _clips(pcm):
            stream = asr.create_stream()
            stream.accept_waveform(16000, clip)
            asr.decode_stream(stream)
            transcript = re.sub(r"<\|[^|]*\|>|[|\r\n]", " ", stream.result.text).strip()
            if len(re.sub(r"[\W_]+", "", transcript)) < 3:
                continue
            path = clips / f"{len(records):04d}.wav"
            with wave.open(str(path), "wb") as output:
                output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                output.writeframes(np.clip(clip * 32768, -32768, 32767).astype("<i2").tobytes())
            records.append({"file": path.name, "text": transcript, "seconds": len(clip) / 16000,
                            "artist": source["artist"], "track": source["title"]})
    if len(records) < 3:
        raise NeedSource("录音中没有足够的清晰单人片段和对齐文本")
    return {"language": language, "records": records}


def child(script: str, work: Path, name: str, environment: dict, *args: str) -> None:
    log = work / (name + ".log")
    with log.open("wb") as stream:
        result = subprocess.run([sys.executable, "-s", script, *args], env=environment,
                                stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, check=False)
    if result.returncode:
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 65536))
            errors = stream.read().lower()
        if b"out of memory" in errors:
            raise OutOfMemory(name)
        raise RuntimeError(name + " did not complete")


def prepare_features(work: Path, data: dict, environment: dict) -> list[Path]:
    clips = work / "clips"
    listing = work / "dataset.list"
    training = data["records"][:-1]
    listing.write_text("".join(f"{clips / row['file']}|character|{data['language']}|{row['text']}\n"
                              for row in training), encoding="utf-8")
    stage_env = environment | {"inp_text": str(listing), "inp_wav_dir": str(clips),
        "exp_name": "character", "opt_dir": str(work), "i_part": "0", "all_parts": "1",
        "is_half": "True", "bert_pretrained_dir": str(BASE / "chinese-roberta-wwm-ext-large"),
        "cnhubert_base_dir": str(BASE / "chinese-hubert-base"), "pretrained_s2G": str(SOVITS),
        "s2config_path": "GPT_SoVITS/configs/s2.json"}
    scripts = ("1-get-text.py", "2-get-hubert-wav32k.py", "3-get-semantic.py")
    for script in scripts:
        child("GPT_SoVITS/prepare_datasets/" + script, work, script.removesuffix(".py"), stage_env)
    rows = work / "2-name2text-0.txt"
    semantics = work / "6-name2semantic-0.tsv"
    text = rows.read_text(encoding="utf-8").splitlines()
    semantic = semantics.read_text(encoding="utf-8").splitlines()
    outputs = [work / "2-name2text.txt", work / "6-name2semantic.tsv"]
    names = {r["file"] for r in training}
    if ({r.split("\t")[0] for r in text if r} != names
            or {r.split("\t")[0] for r in semantic if r} != names):
        raise NeedSource("部分录音缺少文本特征或语义特征")
    outputs[0].write_text("\n".join(text) + "\n", encoding="utf-8")
    outputs[1].write_text("item_name\tsemantic_audio\n" + "\n".join(semantic) + "\n", encoding="utf-8")
    for file in sorted(names):
        outputs.extend([work / "4-cnhubert" / (file + ".pt"), work / "5-wav32k" / file])
        bert = work / "3-bert" / (file + ".pt")
        if bert.is_file():
            outputs.append(bert)
    return outputs


def _plain_config(config: dict) -> dict:
    # The pinned SoVITS exporter writes nested HParams with a mapping interface.
    return {key: _plain_config(value) if hasattr(value, "items") else value
            for key, value in config.items()}


def _save_weights(produced: Path, voice: Path, kind: str) -> list[Path]:
    import torch
    from safetensors.torch import save_file

    # Only an output produced by this owned train process reaches torch.load.
    with produced.open("rb") as stream:
        weight = torch.load(stream, map_location="cpu", weights_only=False)
    config = weight.get("config")
    tensors = weight.get("weight")
    if not callable(getattr(config, "items", None)) or not isinstance(tensors, dict) or not tensors:
        raise RuntimeError("invalid trained " + kind + " export")
    if any(not isinstance(tensor, torch.Tensor) or not torch.isfinite(tensor).all() for tensor in tensors.values()):
        raise RuntimeError("non-finite weight tensors")
    binary = voice / (kind + ".safetensors")
    metadata = voice.parent / (kind + "-config.json")
    save_file({name: value.contiguous().clone() for name, value in tensors.items()}, str(binary))
    write(metadata, _plain_config(config))
    return [binary, metadata]


def train_sovits(work: Path, voice: Path, environment: dict) -> list[Path]:
    settings = read(Path("GPT_SoVITS/configs/s2.json"))
    (work / "logs_s2_v2").mkdir(exist_ok=True)
    exports = work / "sovits-exports"
    exports.mkdir(exist_ok=True)
    settings["train"].update(batch_size=1, epochs=4, fp16_run=True, gpu_numbers="0",
        pretrained_s2G=str(SOVITS), pretrained_s2D=str(PRETRAINED / "s2D2333k.pth"),
        if_save_latest=True, if_save_every_weights=True, save_every_epoch=4)
    settings["model"]["version"] = "v2"
    settings["data"]["exp_dir"] = settings["s2_ckpt_dir"] = str(work)
    settings.update(save_weight_dir=str(exports), name="character", version="v2")
    file = work / "sovits.json"
    write(file, settings)
    child("GPT_SoVITS/s2_train.py", work, "sovits", environment, "--config", str(file))
    weights = sorted(exports.glob("character_e4_*.pth"))
    if len(weights) != 1:
        raise RuntimeError("missing epoch-four SoVITS inference export")
    return _save_weights(weights[0], voice, "sovits")


def train_gpt(work: Path, voice: Path, environment: dict) -> list[Path]:
    import yaml

    settings = yaml.safe_load(Path("GPT_SoVITS/configs/s1longer-v2.yaml").read_text(encoding="utf-8"))
    exports = work / "gpt-exports"
    exports.mkdir(exist_ok=True)
    settings["train"].update(batch_size=1, epochs=5, precision="16-mixed", save_every_n_epoch=5,
        if_save_every_weights=True, if_save_latest=True, half_weights_save_dir=str(exports),
        exp_name="character")
    settings.update(pretrained_s1=str(GPT), train_semantic_path=str(work / "6-name2semantic.tsv"),
        train_phoneme_path=str(work / "2-name2text.txt"), output_dir=str(work / "logs_s1_v2"))
    file = work / "gpt.yaml"
    file.write_text(yaml.safe_dump(settings), encoding="utf-8")
    child("GPT_SoVITS/s1_train.py", work, "gpt", environment, "--config_file", str(file))
    weights = sorted(exports.glob("character-e5.ckpt"))
    if len(weights) != 1:
        raise RuntimeError("missing epoch-five GPT inference export")
    return _save_weights(weights[0], voice, "gpt")


@contextmanager
def _pipeline(voice: Path, runtime: Path):
    import torch
    from safetensors.torch import load_file
    from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config

    config = read(voice / "weights.json")
    # Reconstruct only tensor data and JSON for the upstream loader in a private temporary directory.
    with tempfile.TemporaryDirectory(prefix="infer-", dir=runtime) as temporary:
        work = Path(temporary)
        gpt, sovits = work / "gpt.ckpt", work / "sovits.pth"
        for output, kind in ((gpt, "gpt"), (sovits, "sovits")):
            with output.open("wb") as stream:
                torch.save({"config": config[kind], "weight": load_file(str(voice / (kind + ".safetensors")))},
                           stream)
        settings = {"custom": {"device": "cpu", "is_half": False, "version": "v2",
            "t2s_weights_path": str(gpt), "vits_weights_path": str(sovits),
            "bert_base_path": str(BASE / "chinese-roberta-wwm-ext-large"),
            "cnhuhbert_base_path": str(BASE / "chinese-hubert-base")}}
        # The upstream loader saves configuration on weight changes. Give each loaded
        # engine its own config and keep its reconstructed weights for error recovery.
        import yaml
        file = work / "tts.yaml"
        file.write_text(yaml.safe_dump(settings), encoding="utf-8")
        yield TTS(TTS_Config(str(file)))


def synthesize(pipeline, voice: Path, text: str, language: str) -> bytes:
    import numpy as np

    profile = read(voice / "voice.json")
    values = {"text": text, "text_lang": language, "ref_audio_path": str(voice / "reference.wav"),
        "prompt_text": profile["reference_text"], "prompt_lang": profile["reference_language"],
        "text_split_method": "cut5", "batch_size": 1, "parallel_infer": False,
        "streaming_mode": False, "seed": 1234}
    rate, chunks = 0, []
    for rate, audio in pipeline.run(values):
        chunks.append(np.asarray(audio, dtype="<i2"))
    if not chunks or not rate or not any(np.any(chunk) for chunk in chunks):
        raise RuntimeError("no audible synthesis produced")
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        for chunk in chunks:
            wav.writeframes(chunk.tobytes())
    return output.getvalue()


def checkpoint(work: Path, resume: Path | None, name: str, action):
    target = work / (name + "-receipt.json")
    if resume is not None and (resume / target.name).is_file():
        files = read(resume / target.name)
        for relative, sha in files.items():
            source = resume / relative
            if (not source.resolve().is_relative_to(resume.resolve()) or not source.is_file()
                    or digest(source) != sha):
                raise RuntimeError("invalid prior stage receipt")
        for relative in files:
            dest = work / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(resume / relative, dest)
    else:
        files = {file.relative_to(work).as_posix(): digest(file) for file in action()
                 if file.is_file() and file.stat().st_size}
        if not files:
            raise RuntimeError("stage did not write outputs")
    write(target, files)


def train(config: dict, work: Path, job: Path, resume: Path | None) -> None:
    import torch

    if not torch.cuda.is_available() or torch.version.cuda != "12.1":
        raise RuntimeError("the pinned Windows CUDA runtime is unavailable")
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    upstream = Path(config["upstream"])
    enter(upstream)
    environment = dict(os.environ) | {
        "_CUDA_VISIBLE_DEVICES": "0",
        "PYTHONPATH": os.pathsep.join((str(upstream), str(upstream / "GPT_SoVITS"))),
    }
    environment["PATH"] = str(Path(config["ffmpeg"]).parent) + os.pathsep + os.environ.get("PATH", "")
    voice = work / "voice"
    voice.mkdir(exist_ok=True)
    stage(job, "voice_dataset")

    def prepare():
        dataset = build_dataset(config, read(work / "sources.json")["sources"], work)
        write(work / "dataset.json", dataset)
        return [work / "dataset.json", *(work / "clips").glob("*.wav")]

    checkpoint(work, resume, "dataset", prepare)
    data = read(work / "dataset.json")
    stage(job, "voice_features")
    checkpoint(work, resume, "features", lambda: prepare_features(work, data, environment))
    stage(job, "voice_sovits")
    checkpoint(work, resume, "sovits-training", lambda: train_sovits(work, voice, environment))
    stage(job, "voice_gpt")
    checkpoint(work, resume, "gpt-training", lambda: train_gpt(work, voice, environment))
    stage(job, "voice_preview")
    reference = data["records"][-1]
    shutil.copyfile(work / "clips" / reference["file"], voice / "reference.wav")
    write(voice / "voice.json", {"reference_text": reference["text"], "reference_language": data["language"],
        "sources": read(work / "sources.json")["sources"], "upstream": PIN, "so_vits_epochs": 4, "gpt_epochs": 5,
        "training_seconds": round(sum(r["seconds"] for r in data["records"][:-1]), 2)})
    write(voice / "weights.json", {kind: read(work / (kind + "-config.json")) for kind in ("gpt", "sovits")})
    with _pipeline(voice, work) as engine:
        for line, file in zip(SAMPLE_LINES, ("preview.wav", "preview-2.wav")):
            (voice / file).write_bytes(synthesize(engine, voice, line, "zh"))


def serve(config: dict) -> None:
    from fastapi import FastAPI, HTTPException, Request, Response
    import uvicorn

    # FastAPI evaluates postponed annotations against module globals.
    globals()["Request"] = Request
    enter(Path(config["upstream"]))
    library = Path(config["library"]) / ".voices"
    runtime = Path(config["runtime"])
    lock = Lock()
    port = config["port"]
    auth = os.environ["AGENTSTRATA_GPT_SOVITS_AUTH"]
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    voice_context = ExitStack()
    selected = None
    result = None

    def engine(identifier: str):
        nonlocal selected, result, voice_context
        if selected == identifier:
            return result
        directory = library / identifier
        if directory.resolve() != directory or not directory.is_dir():
            raise ValueError("voice directory escaped the local library")
        receipt = read(directory / "manifest.json")
        content = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(content).hexdigest() != identifier:
            raise ValueError("invalid voice receipt")
        for name in ("gpt.safetensors", "sovits.safetensors", "weights.json", "voice.json", "reference.wav"):
            path = directory / name
            if path.is_symlink() or digest(path) != receipt["files"][name]["sha256"]:
                raise ValueError("invalid voice bytes")
        with ExitStack() as candidate:
            pipeline = candidate.enter_context(_pipeline(directory, runtime))
            voice_context.close()
            voice_context = candidate.pop_all()
            selected, result = identifier, (directory, pipeline)
        return result

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/tts")
    def tts(request: Request, text: str, ref_audio_path: str, text_lang: str = "zh"):
        if (request.headers.get("host") != "127.0.0.1:" + str(port)
                or not hmac.compare_digest(request.headers.get("authorization", ""), auth)):
            raise HTTPException(status_code=403)
        if not re.fullmatch(r"[a-f0-9]{64}", ref_audio_path) or not text.strip() or len(text) > 12000:
            raise HTTPException(status_code=400)
        if text_lang not in {"zh", "ja", "en", "yue", "ko", "auto"}:
            raise HTTPException(status_code=400)
        with lock:
            try:
                directory, pipeline = engine(ref_audio_path)
                return Response(synthesize(pipeline, directory, text, text_lang), media_type="audio/wav")
            except (ValueError, OSError, KeyError, RuntimeError):
                raise HTTPException(status_code=503, detail="角色音色无法合成") from None

    try:
        uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)
    finally:
        voice_context.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("train", "serve"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--work", type=Path)
    parser.add_argument("--job", type=Path)
    parser.add_argument("--resume", type=Path)
    args = parser.parse_args()
    config = read(args.config)
    try:
        if args.mode == "serve":
            serve(config)
        else:
            started = time.monotonic()
            train(config, args.work, args.job, args.resume)
            print("Training seconds:", round(time.monotonic() - started, 2))
        return 0
    except NeedSource as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except OutOfMemory as exc:
        print("Out of memory during", str(exc), file=sys.stderr)
        return 4
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        print("Voice process failed:", type(exc).__name__, str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
