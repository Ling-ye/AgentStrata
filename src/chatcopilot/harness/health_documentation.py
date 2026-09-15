"""Host-owned evidence for narrowly scoped documentation candidates."""
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path
from urllib.parse import unquote, urlsplit

from chatcopilot.harness.health_policy import policy_path, requires_regression


def documentation_path(name: str) -> bool:
    path = Path(name)
    if policy_path(name) or requires_regression(name):
        return False
    if any("prompt" in part or "policy" in part or Path(part).stem in {"authorization", "config", "configs", "settings"} for part in path.parts):
        return False
    return (name.startswith("docs/") and path.suffix == ".md") or (name.startswith("src/") and path.suffix == ".py")


def eligible(findings: list[dict]) -> bool:
    return bool(findings) and all(row["rule_id"] == "documentation" and documentation_path(row["path"])
                                  for row in findings)


_DIRECTIVE = re.compile(r"^#!|coding\s*[:=]|\b(?:type|noqa|ruff|fmt|isort|pylint|mypy|pyright|pragma|nosec|doctest)\b", re.I)


def _python_shape(raw: bytes):
    compile(raw, "<documentation-candidate>", "exec", dont_inherit=True)
    tree = ast.parse(raw, type_comments=True)
    doc = tree.body[0] if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str) else None
    tokens, directives = [], []
    for token in tokenize.tokenize(io.BytesIO(raw).readline):
        if token.type == tokenize.COMMENT:
            if _DIRECTIVE.search(token.string):
                directives.append(token.string)
            continue
        if doc and doc.lineno <= token.start[0] <= doc.end_lineno and token.type in {tokenize.STRING, tokenize.NEWLINE}:
            continue
        if token.type not in {tokenize.NL, tokenize.ENDMARKER}:
            tokens.append((token.type, token.string))
    if doc:
        tree.body.pop(0)
    return ast.dump(tree, include_attributes=False), tokens, directives, ast.get_docstring(ast.parse(raw))


def _qualified(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _qualified(node.value) + "." + node.attr
    return ""


def description_consumed(root: Path, name: str, manifest: dict) -> bool:
    """Detect direct module-description consumers; independent review covers dynamic use."""
    module = name.removeprefix("src/").removesuffix(".py").replace("/", ".").removesuffix(".__init__")
    for other in manifest:
        if not other.endswith(".py"):
            continue
        try:
            tree = ast.parse((root / other).read_bytes())
        except SyntaxError:
            # An unreadable consumer cannot establish the lightweight boundary.
            return True
        aliases = {module}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                aliases.update(a.asname for a in node.names if a.name == module and a.asname)
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    package = other.removeprefix("src/").split("/")[:-1]
                    prefix = ".".join(package[:len(package) - node.level + 1] + ([prefix] if prefix else []))
                if prefix == module and any(a.name in {"__doc__", "*"} for a in node.names):
                    return True
                aliases.update(a.asname or a.name for a in node.names if prefix + "." + a.name == module)
        for node in ast.walk(tree):
            if other == name and isinstance(node, ast.Name) and node.id == "__doc__":
                return True
            if isinstance(node, ast.Attribute) and node.attr == "__doc__" and _qualified(node.value) in aliases:
                return True
            if isinstance(node, ast.Call) and node.args and _qualified(node.args[0]) in aliases:
                if _qualified(node.func).split(".")[-1] in {"getdoc", "getattr", "help", "vars"}:
                    return True
    return False


def classify(before: Path, after: Path, names: list[str], before_manifest: dict, after_manifest: dict) -> dict:
    for name in names:
        if not documentation_path(name):
            return {"eligible": False, "reason": f"{name} 不属于普通说明文本"}
        if name not in before_manifest or name not in after_manifest or before_manifest[name]["executable"] != after_manifest[name]["executable"]:
            return {"eligible": False, "reason": "文件新增、删除或可执行位变化需标准验证"}
        if name.endswith(".py"):
            try:
                old, new = _python_shape((before / name).read_bytes()), _python_shape((after / name).read_bytes())
            except (SyntaxError, UnicodeError, tokenize.TokenError):
                return {"eligible": False, "reason": f"{name} 不能形成有效 Python 语法证据"}
            if old[:3] != new[:3]:
                return {"eligible": False, "reason": f"{name} 的代码、函数说明或检查指令有变化"}
            if old[3] != new[3] and (description_consumed(before, name, before_manifest) or description_consumed(after, name, after_manifest)):
                return {"eligible": False, "reason": f"{name} 的模块说明存在消费关系"}
    return {"eligible": True, "kind": "documentation_only", "paths": names,
            "reason": "宿主确认仅普通说明变化；Python 可执行结构、token 与检查指令一致，语义由独立审查确认"}


def markdown_links(root: Path, names: list[str]) -> list[str]:
    failures = []
    for name in names:
        if not name.endswith(".md"):
            continue
        text = (root / name).read_text(encoding="utf-8")
        # Ignore fenced examples and inline code; inspect inline and reference links.
        text = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$", "", text)
        text = re.sub(r"`[^`\n]*`", "", text)
        targets = re.findall(r"\]\(\s*(<[^>]*>|[^\s)]+)", text)
        targets += re.findall(r"(?m)^\s*\[[^\]]+\]:\s*(<[^>]*>|\S+)", text)
        for target in targets:
            url = urlsplit(target.strip("<>"))
            if url.scheme or url.netloc or not url.path:
                continue
            path = ((root if url.path.startswith("/") else (root / name).parent) / unquote(url.path).lstrip("/")).resolve()
            if not path.is_relative_to(root.resolve()) or not path.exists():
                failures.append(f"{name}: 本地链接不存在或超出源码范围：{target}")
    return failures
