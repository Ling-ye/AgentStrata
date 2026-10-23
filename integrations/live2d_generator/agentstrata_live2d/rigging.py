"""Pinned image2live2d adapter; heavy imports stay outside the host and request types."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import struct

from .repository import extract_zip, image_info, private_directory, write_json
from .types import GenerationError, MAX_EXPANDED, MAX_FILES, MAX_PIXELS

REVISION = "714e7cc9191f1ef6c9a1732c80e8b566ec127d6b"


def prepare_layers(source: Path, work: Path):
    from image2live2d.core import decompose

    if source.suffix.lower() == ".zip":
        extract_zip(source, work, suffixes={".png"})
        pngs = list(work.rglob("*.png"))
        if not pngs or len({p.parent for p in pngs}) != 1:
            raise GenerationError("图层 ZIP 必须在同一目录包含语义命名的 PNG 图层")
        sizes = {image_info(p) for p in pngs}
        if len(sizes) != 1:
            raise GenerationError("PNG 图层必须使用相同画布尺寸")
        width, height = next(iter(sizes))
        if len(pngs) * width * height * 4 > MAX_EXPANDED:
            raise GenerationError("图层展开像素内存超过 256 MiB")
        return decompose.from_layer_dir(pngs[0].parent), pngs[0].parent
    if source.suffix.lower() != ".psd":
        raise GenerationError("绑定器仅接受 PSD 或图层 ZIP")
    with source.open("rb") as stream:
        header = stream.read(26)
    if len(header) != 26 or header[:6] != b"8BPS\x00\x01":
        raise GenerationError("输入不是受支持的 PSD 文件")
    height, width = struct.unpack(">II", header[14:22])
    if not width or not height or width * height > MAX_PIXELS:
        raise GenerationError("PSD 画布尺寸超限")
    from psd_tools import PSDImage

    psd = PSDImage.open(source)
    layers = list(psd.descendants())
    if (len(layers) > MAX_FILES or len(layers) * width * height * 4 > MAX_EXPANDED
            or any(p.width * p.height > MAX_PIXELS for p in layers)):
        raise GenerationError("PSD 图层数量或尺寸超限")
    return decompose.from_psd(source, work), work


class AutomaticRigger:
    def build(self, source: Path, destination: Path, work: Path) -> dict:
        from image2live2d.pipeline import rig_from_stack
        from image2live2d.core.qa import evaluate
        from image2live2d.backends.live2d import cdi3, model3, motion3, physics3
        from image2live2d.backends.live2d.moc3_binary import write_moc3
        from image2live2d.backends.live2d.moc3_emit import build_atlas, rig_to_moc3
        from PIL import Image

        private_directory(work)
        stack, layers = prepare_layers(source, work)
        roles = {layer.semantic_role.value for layer in stack.layers}
        if "face_base" not in roles or not roles.intersection({"eye_l", "eye_r", "pupil_l", "pupil_r"}):
            raise GenerationError("素材缺少可识别的脸部和眼睛图层，无法建立角色绑定")
        rig = rig_from_stack(stack, name="avatar")
        private_directory(destination / "textures")
        atlas, uv = build_atlas(rig, layers)
        atlas.save(destination / "textures/atlas.png")
        (destination / "avatar.moc3").write_bytes(write_moc3(rig_to_moc3(rig, atlas_uv=uv)))
        physics = None
        if rig.physics:
            physics = "avatar.physics3.json"
            write_json(destination / physics, physics3.physics3(rig))
        write_json(destination / "avatar.cdi3.json", cdi3.cdi3(rig))
        motions = {}
        # The all-parameter QA sweep is evidence, not a user-facing default animation.
        for animation in rig.animations:
            if animation.name not in {"idle", "head_turn", "nod", "wave"}:
                continue
            name = f"avatar.{animation.name}.motion3.json"
            write_json(destination / name, motion3.motion3(animation))
            motions.setdefault(animation.name.capitalize(), []).append(name)
        doc = model3.model3(rig, moc="avatar.moc3", textures=["textures/atlas.png"],
                            physics=physics, display_info="avatar.cdi3.json", motions=motions or None)
        parameters = {p.id for p in rig.parameters}
        # Standard parameters already authored by the rig; never advertise invented actions.
        presets = {"neutral": {}, "joy": {"ParamMouthForm": 1, "ParamEyeLOpen": 0.75, "ParamEyeROpen": 0.75},
                   "sadness": {"ParamMouthForm": -1, "ParamBrowLY": -0.5, "ParamBrowRY": -0.5},
                   "surprise": {"ParamMouthOpenY": 0.7, "ParamBrowLY": 0.7, "ParamBrowRY": 0.7}}
        used = sorted({k for preset in presets.values() for k in preset if k in parameters})
        defaults = {p.id: p.default for p in rig.parameters}
        expressions = []
        for name, values in presets.items():
            expression = {"Type": "Live2D Expression", "FadeInTime": 0.3, "FadeOutTime": 0.3,
                          "Parameters": [{"Id": k, "Value": values.get(k, defaults[k]), "Blend": "Overwrite"}
                                         for k in used]}
            filename = f"{name}.exp3.json"
            write_json(destination / filename, expression)
            expressions.append({"Name": name, "File": filename})
        doc["FileReferences"]["Expressions"] = expressions
        write_json(destination / "avatar.model3.json", doc)
        preview = Image.new("RGBA", (stack.canvas_width, stack.canvas_height))
        for layer in sorted(stack.layers, key=lambda x: x.draw_order):
            if layer.semantic_role.value in {"eye_closed_l", "eye_closed_r", "blush", "mouth_cavity"}:
                continue
            with Image.open(layer.texture_path) as image:
                preview.alpha_composite(image.convert("RGBA"))
        preview.thumbnail((768, 1024))
        preview.save(destination / "preview.png")
        report = asdict(evaluate(rig, "avatar"))
        write_json(destination / "rig-report.json", report)
        return {"engine": "image2live2d", "revision": REVISION,
                "layers": len(stack.layers), "parameters": len(rig.parameters), "qa": report,
                "quality": "automatic-draft", "sdk_validation": "not-run"}
