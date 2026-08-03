"""Skill manifest 数据模型 + 解析 + body 读取 + 索引渲染。

每个机器人的 ``skills/manifest.yaml`` 列举其启用的 skill id；每个 id 对应同目录
下 ``<id>/SKILL.md``，文件以 YAML frontmatter（``name`` + ``description``）起始。
本模块负责把 manifest 解析成有序的 ``SkillIndexEntry`` 列表，并提供按需读取 body
与渲染 system prompt 索引片段的能力。

设计：
- 解析放在 botspec/（配置态），运行时注册表与 ``read_bot_skill`` 工具放在 agent/。
- 解析失败统一抛 ``SkillManifestError``，由 ``botspec.loader._validate_skills_manifest``
  转译成 ValidationIssue。
"""
from __future__ import annotations

from pathlib import Path

from chatcopilot.contracts.skills import SkillIndexEntry, read_skill_body, render_skill_index_section


class SkillManifestError(ValueError):
    """Skill manifest 解析或 SKILL.md frontmatter 错误。"""


def load_skill_index(manifest_path: str | Path) -> tuple[SkillIndexEntry, ...]:
    """解析 manifest.yaml，按声明顺序返回启用 skill 的索引条目。

    manifest 形如::

        skills:
          - id: alpha
          - id: beta
            enabled: false
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 部署期保证依赖
        raise RuntimeError(
            "缺少 PyYAML 依赖，请先安装：python -m pip install PyYAML"
        ) from exc

    manifest = Path(manifest_path).expanduser().resolve()
    if not manifest.is_file():
        raise SkillManifestError(f"skills manifest 不存在: {manifest}")

    with manifest.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise SkillManifestError(f"skills manifest 顶层必须是 mapping: {manifest}")

    raw_skills = data.get("skills") or []
    if not isinstance(raw_skills, list):
        raise SkillManifestError(f"skills.skills 必须是 list: {manifest}")

    base = manifest.parent
    seen: set[str] = set()
    entries: list[SkillIndexEntry] = []
    for index, raw in enumerate(raw_skills):
        if not isinstance(raw, dict):
            raise SkillManifestError(
                f"skills[{index}] 必须是 mapping，例如 `- id: foo`: {raw!r}"
            )
        skill_id = str(raw.get("id", "")).strip()
        if not skill_id:
            raise SkillManifestError(f"skills[{index}].id 不能为空")
        if skill_id in seen:
            raise SkillManifestError(f"skill id 重复声明: {skill_id}")
        seen.add(skill_id)

        if raw.get("enabled") is False:
            continue

        body_path = base / skill_id / "SKILL.md"
        if not body_path.is_file():
            raise SkillManifestError(
                f"skill {skill_id} 缺少 SKILL.md: 期望路径 {body_path}"
            )
        name, description = _parse_skill_frontmatter(body_path)
        entries.append(
            SkillIndexEntry(
                id=skill_id,
                name=name,
                description=description,
                body_path=body_path,
            )
        )
    return tuple(entries)



# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------
def _parse_skill_frontmatter(path: Path) -> tuple[str, str]:
    raw = path.read_text(encoding="utf-8")
    frontmatter = _extract_frontmatter(raw)
    if frontmatter is None:
        raise SkillManifestError(
            f"{path} 缺少 YAML frontmatter（应以 `---` 起始声明 name 与 description）"
        )

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 PyYAML 依赖") from exc

    data = yaml.safe_load(frontmatter) or {}
    if not isinstance(data, dict):
        raise SkillManifestError(f"{path} frontmatter 必须是 mapping")

    name = str(data.get("name", "")).strip()
    description = str(data.get("description", "")).strip()
    if not name:
        raise SkillManifestError(f"{path} frontmatter 缺少 name")
    if not description:
        raise SkillManifestError(f"{path} frontmatter 缺少 description")
    return name, description


def _extract_frontmatter(raw: str) -> str | None:
    text = raw.lstrip("\ufeff")
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[1:idx])
    return None


def _strip_frontmatter(raw: str) -> str:
    text = raw.lstrip("\ufeff")
    if not text.startswith("---"):
        return raw
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return raw
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[idx + 1 :])
    return raw


__all__ = [
    "SkillIndexEntry",
    "SkillManifestError",
    "load_skill_index",
    "read_skill_body",
    "render_skill_index_section",
]
