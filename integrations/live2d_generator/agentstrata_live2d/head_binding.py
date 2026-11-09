"""Restrained horizontal turns for automatically rigged single-view artwork."""
from __future__ import annotations


# The generated face has no separately authored profile silhouette. Keep its yaw
# displacement modest while retaining the full standard tracking parameter range.
HEAD_YAW_SCALE = 0.4


def soften_head_yaw(moc) -> bool:
    """Rescale only yaw, relative to each keyform's matching pitch/roll pose.

    Applied before neck binding so the neck follows the final head deformation.
    Blending against the all-zero pose would also weaken pitch/roll and is wrong.
    """
    sections = moc.sections
    deformers, warps = sections["deformers"], sections["warpDeformers"]
    if "D_HEAD" not in deformers["ids"]:
        return False
    warp = deformers["specificSourcesIndices"][deformers["ids"].index("D_HEAD")]
    binding = warps["keyformBindingSourcesIndices"][warp]
    bindings = sections["keyformBindings"]
    begin = bindings["parameterBindingIndexSourcesBeginIndices"][binding]
    count = bindings["parameterBindingIndexSourcesCounts"][binding]
    axes = sections["parameterBindingIndices"]["bindingSourcesIndices"][begin:begin + count]
    parameters, parameter_bindings = sections["parameters"], sections["parameterBindings"]
    stride = 1
    for parameter in axes:
        size = parameter_bindings["keysSourcesCounts"][parameter]
        if parameters["ids"][parameter] == "ParamAngleX":
            start = parameter_bindings["keysSourcesBeginIndices"][parameter]
            keys = sections["keys"]["values"][start:start + size]
            neutral = keys.index(parameters["defaultValues"][parameter])
            break
        stride *= size
    else:
        return False

    positions = sections["keyformPositions"]["xys"]
    first = warps["keyformSourcesBeginIndices"][warp]
    count = warps["keyformSourcesCounts"][warp]
    vertices = warps["vertexCounts"][warp]
    starts = [value // 2 for value in
              sections["warpDeformerKeyforms"]["keyformPositionSourcesBeginIndices"][first:first + count]]
    grids = [positions[start:start + vertices] for start in starts]
    for index, (start, grid) in enumerate(zip(starts, grids)):
        yaw_key = (index // stride) % size
        if yaw_key == neutral:
            continue
        reference = grids[index + (neutral - yaw_key) * stride]
        positions[start:start + vertices] = [
            (ax + HEAD_YAW_SCALE * (x - ax), ay + HEAD_YAW_SCALE * (y - ay))
            for (x, y), (ax, ay) in zip(grid, reference)
        ]
    return True
