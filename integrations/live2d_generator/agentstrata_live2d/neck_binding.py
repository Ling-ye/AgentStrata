"""Connect the pinned image2live2d emitter's fixed neck to its authored head warp.

Operate on the emitter's structured MOC before serialization, never on an installed
model. Reuse the head's keyforms and parameter binding, so yaw/pitch/roll compose
exactly once and no extra tracking parameters or runtime physics are needed.
"""
from __future__ import annotations

import math


def _default_keyform(sections, binding: int) -> int:
    """The pinned emitter orders keyforms with the first parameter varying fastest."""
    bindings = sections["keyformBindings"]
    start = bindings["parameterBindingIndexSourcesBeginIndices"][binding]
    count = bindings["parameterBindingIndexSourcesCounts"][binding]
    indices = sections["parameterBindingIndices"]["bindingSourcesIndices"][start:start + count]
    parameters = sections["parameters"]
    parameter_bindings = sections["parameterBindings"]
    index, stride = 0, 1
    for parameter in indices:
        begin = parameter_bindings["keysSourcesBeginIndices"][parameter]
        length = parameter_bindings["keysSourcesCounts"][parameter]
        keys = sections["keys"]["values"][begin:begin + length]
        index += keys.index(parameters["defaultValues"][parameter]) * stride
        stride *= length
    return index


def _sample(grid, rows: int, columns: int, u: float, v: float):
    """Bilinear sample, with edge-cell extrapolation for points beyond the head grid."""
    x, y = u * columns, v * rows
    c = max(0, min(columns - 1, math.floor(x)))
    r = max(0, min(rows - 1, math.floor(y)))
    x, y = x - c, y - r
    a, b = r * (columns + 1) + c, (r + 1) * (columns + 1) + c
    return tuple(
        (1 - y) * ((1 - x) * grid[a][axis] + x * grid[a + 1][axis])
        + y * ((1 - x) * grid[b][axis] + x * grid[b + 1][axis])
        for axis in (0, 1)
    )


def bind_neck_to_head(moc, rig) -> int:
    """Add one neck lattice: follow the head attachment above, pin the collar below.

    Bounds come from this character's neutral meshes. Existing neck keyforms
    (breathing/body motion), textures, draw order and all head geometry are retained.
    Returns the number of bound neck meshes; rigs without a neck need no correction.
    """
    sections = moc.sections
    deformers, warps = sections["deformers"], sections["warpDeformers"]
    meshes = sections["artMeshes"]
    roles = {part.id: part.semantic_role.value for part in rig.parts}
    necks = [i for i, name in enumerate(meshes["ids"]) if roles[name] == "neck"]
    faces = [i for i, name in enumerate(meshes["ids"]) if roles[name] == "face_base"]
    if not necks or not faces or "D_HEAD" not in deformers["ids"]:
        return 0

    head = deformers["ids"].index("D_HEAD")
    warp = deformers["specificSourcesIndices"][head]
    binding = warps["keyformBindingSourcesIndices"][warp]
    rows, columns = warps["rows"][warp], warps["columns"][warp]
    positions = sections["keyformPositions"]["xys"]
    warp_keys = sections["warpDeformerKeyforms"]
    first = warps["keyformSourcesBeginIndices"][warp]
    count = warps["keyformSourcesCounts"][warp]
    size = warps["vertexCounts"][warp]
    grids = []
    for key in range(first, first + count):
        begin = warp_keys["keyformPositionSourcesBeginIndices"][key] // 2
        grids.append(positions[begin:begin + size])
    rest = grids[_default_keyform(sections, binding)]
    hx, hy = rest[0]
    hw, hh = rest[-1][0] - hx, rest[-1][1] - hy

    def mesh_points(index, key=None):
        if key is None:
            key = _default_keyform(sections, meshes["keyformBindingSourcesIndices"][index])
        key += meshes["keyformSourcesBeginIndices"][index]
        begin = sections["artMeshKeyforms"]["keyformPositionSourcesBeginIndices"][key] // 2
        return begin, positions[begin:begin + meshes["vertexCounts"][index]]

    neck = [point for i in necks for point in mesh_points(i)[1]]
    # Head vertices are in D_HEAD local coordinates; neck vertices are in model space.
    face = [_sample(rest, rows, columns, u, v) for i in faces for u, v in mesh_points(i)[1]]
    nx0, nx1 = min(x for x, _ in neck), max(x for x, _ in neck)
    ny0, ny1 = min(y for _, y in neck), max(y for _, y in neck)
    face_above = sum(y for _, y in face) / len(face) < (ny0 + ny1) / 2
    collar = ny1 if face_above else ny0
    jaw = max(y for _, y in face) if face_above else min(y for _, y in face)
    # Non-overlapping/degenerate source layers cannot be repaired by a continuity warp.
    if not ny0 < jaw < ny1:
        return 0

    # A neck-sized lattice resolves the transition even on long-haired rigs.
    # Cover every authored body/breath keyform before converting them to lattice coordinates.
    all_points = [point for i in necks for key in range(meshes["keyformSourcesCounts"][i])
                  for point in mesh_points(i, key)[1]]
    x0, x1 = min(x for x, _ in all_points), max(x for x, _ in all_points)
    top = ny0 if face_above else ny1
    step = abs(top - collar) / rows
    # Put the collar exactly on a row. Otherwise interpolation across the pinned /
    # moving boundary pulls it along even though its analytic influence is zero.
    y0 = collar + math.floor((min(y for _, y in all_points) - collar) / step) * step
    y1 = collar + math.ceil((max(y for _, y in all_points) - collar) / step) * step
    neck_rows = round((y1 - y0) / step)
    width, height = x1 - x0, y1 - y0
    if width <= 0 or height <= 0:
        return 0
    lattice = [(x0 + width * c / columns, y0 + height * r / neck_rows)
               for r in range(neck_rows + 1) for c in range(columns + 1)]
    start = len(warp_keys["opacities"])
    center = (nx0 + nx1) / 2
    direction = -1 if face_above else 1
    minimum_height = min(abs(top - collar), 1 / moc.canvas["pixelsPerUnit"])
    for grid in grids:
        warp_keys["opacities"].append(1.0)
        warp_keys["keyformPositionSourcesBeginIndices"].append(2 * len(positions))
        tx, ty = _sample(grid, rows, columns, (center - hx) / hw, (top - hy) / hh)
        # Share a rigid top-edge displacement across the neck width. Applying the
        # face's squash/roll to individual neck columns can fold a short neck.
        # When a down+roll pose carries the attachment through the collar, compress
        # the now-occluded neck to one model pixel instead of reversing its mesh.
        ty = collar + direction * max(minimum_height, direction * (ty - collar))
        for x, y in lattice:
            weight = max(0.0, min(1.0, (y - collar) / (top - collar)))
            positions.append((x + weight * (tx - center), y + weight * (ty - top)))
        # The pinned writer/native Core requires each keyform to start at an 8-pair boundary.
        positions.extend([(0.0, 0.0)] * (-len(positions) % 8))

    deformer_index, warp_index = len(deformers["ids"]), len(warps["rows"])
    for field, value in {
        "ids": "D_NECK", "keyformBindingSourcesIndices": binding,
        "isVisible": 1, "isEnabled": 1, "parentPartIndices": meshes["parentPartIndices"][necks[0]],
        "parentDeformerIndices": -1, "types": 0, "specificSourcesIndices": warp_index,
    }.items():
        deformers[field].append(value)
    deformers["runtimeSpace0"] += b"\0" * 8
    for field, value in {
        "keyformBindingSourcesIndices": binding, "keyformSourcesBeginIndices": start,
        "keyformSourcesCounts": count, "vertexCounts": len(lattice), "rows": neck_rows, "columns": columns,
    }.items():
        warps[field].append(value)
    for index in necks:
        meshes["parentDeformerIndices"][index] = deformer_index
        for key in range(meshes["keyformSourcesCounts"][index]):
            begin, points = mesh_points(index, key)
            positions[begin:begin + len(points)] = [((x - x0) / width, (y - y0) / height) for x, y in points]
    moc.counts.update(deformers=deformer_index + 1, warpDeformers=warp_index + 1,
                      warpDeformerKeyforms=len(warp_keys["opacities"]), keyformPositions=2 * len(positions))
    return len(necks)
