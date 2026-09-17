#!/usr/bin/env python3
"""Read-only checks for maintained documentation and its source links."""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
from dataclasses import dataclass, field
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import unicodedata
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
ROOT_DOCS = {"README.md", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md", "SUPPORT.md", "CODE_OF_CONDUCT.md", "CHANGELOG.md", ".github/pull_request_template.md"}
SKIP_DIRS = {".git", ".cache", ".worktrees", ".venv", "node_modules", "dist", "build", "__pycache__", "vendor", "fixtures", "prompts", "skills"}
SOURCE_ROOTS = {"src", "tests", "scripts", "console", "bots", "deploy", "requirements", ".github"}
_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_REFERENCE = re.compile(r'^ {0,3}\[([^\]]+)\]:\s*(<[^>]+>|\S+)')
_RESULT = re.compile(r"\b\d[\d,]*\s+(?:passed|failed|skipped|subtests?\s+passed|tests?\s+passed)\b", re.I)
_RUN_HEADING = re.compile(r"^\s*(?:#{1,6}\s*)?(?:20\d\d[-/]\d{1,2}[-/]\d{1,2}[，,:：\s].*(?:实际验证|本次验证|验证记录|完成验证|执行结果)|(?:本次|实际|执行|验证|验收)[^\n]{0,24}20\d\d[-/]\d{1,2}[-/]\d{1,2}|(?:verification completed|recorded|latest execution)\b)", re.I)
_TEMP_REPORT = re.compile(r"(?:\.cache/|/tmp/)[^\s`<>]+", re.I)
_EXAMPLE = re.compile(r"示例|例子|样例|反例|夹具|基准|example|fixture|benchmark", re.I)


@dataclass
class Link:
    target: str
    line: int
    source: bool = False


@dataclass
class Document:
    name: str
    text: str
    anchors: set[str] = field(default_factory=set)
    links: list[Link] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    prose: list[str] = field(default_factory=list)
    paragraphs: list[tuple[int, str]] = field(default_factory=list)


def issue(rule: str, path: str, line: int, message: str, **extra) -> dict:
    return {"rule": rule, "path": path, "line": line, "message": message, **extra}


def authored_document(name: str) -> bool:
    """Maintainer docs only; executable prompts, cases and third-party notices are data."""
    p = PurePosixPath(name)
    if name in ROOT_DOCS:
        return True
    if any(part in SKIP_DIRS for part in p.parts):
        return False
    return (name.startswith(("docs/", "specs/")) and p.suffix == ".md"
            or p.name == "README.md" and p.parts[0] in {"src", "bots", "console", "deploy"})


def discover(root: Path) -> list[str]:
    names = [name for name in sorted(ROOT_DOCS) if (root / name).is_file()]
    for top in ("docs", "specs", "src", "bots", "console", "deploy"):
        directory = root / top
        if not directory.exists():
            continue
        for current, dirs, files in os.walk(directory, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (Path(current) / d).is_symlink())
            for filename in sorted(files):
                name = (Path(current) / filename).relative_to(root).as_posix()
                if authored_document(name):
                    names.append(name)
    return sorted(set(names))


def slug(heading: str) -> str:
    heading = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", heading)
    heading = re.sub(r"<[^>]+>", "", heading)
    heading = html.unescape(heading).replace("`", "").lower()
    return "".join(c for c in heading if c in "-_ " or not unicodedata.category(c).startswith(("P", "S", "C"))).replace(" ", "-")


def inline_targets(line: str) -> list[str]:
    """Consume balanced destinations, including angle paths, titles and image links."""
    result = []
    for match in re.finditer(r"\]\(\s*", line):
        start = match.end()
        if start == len(line):
            continue
        if line[start] == "<":
            end = line.find(">", start + 1)
            if end >= 0:
                result.append(line[start + 1:end])
            continue
        cursor, depth = start, 0
        while cursor < len(line):
            char = line[cursor]
            if char == "\\" and cursor + 1 < len(line):
                cursor += 2
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                if not depth:
                    break
                depth -= 1
            elif char.isspace() and not depth:
                break
            cursor += 1
        result.append(re.sub(r"\\([()])", r"\1", line[start:cursor]))
    result.extend(re.findall(r'<(?:a|img)\b[^>]*(?:href|src)=[\"\x27]([^\"\x27]+)', line, re.I))
    return result


def table_separator(line: str) -> bool:
    return "|" in line and all(re.fullmatch(r":?-{3,}:?", part.strip())
                               for part in line.strip().strip("|").split("|"))


def parse(name: str, text: str) -> Document:
    doc = Document(name, text)
    references, visible, headings, occurrences = {}, [], [], defaultdict(int)
    fence, frontmatter = "", text.startswith("---\n")
    source_level = None
    in_table = False
    paragraph: list[str] = []
    paragraph_line = 1

    def flush_paragraph() -> None:
        if paragraph:
            doc.paragraphs.append((paragraph_line, " ".join(paragraph)))
            paragraph.clear()

    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        if frontmatter:
            if number > 1 and line.strip() == "---":
                frontmatter = False
            continue
        boundary = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if boundary:
            flush_paragraph()
            token = boundary[1]
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not boundary[2].strip():
                fence = ""
            continue
        if fence:
            # Code samples are not executed or treated as links. Result transcripts in
            # non-example sections remain detectable even when fenced as shell output.
            example = any(_EXAMPLE.search(title) for _, title in headings)
            if _RESULT.search(line) and not example:
                doc.issues.append(issue("run-record", name, number, "执行计数应保留在交付说明或 CI 日志"))
            continue
        match = _HEADING.match(line)
        if match:
            flush_paragraph()
            level, title = len(match[1]), match[2]
            headings = [(n, s) for n, s in headings if n < level] + [(level, title)]
            base = slug(title)
            count = occurrences[base]
            occurrences[base] += 1
            doc.anchors.add(base if count == 0 else f"{base}-{count}")
            if source_level is not None and level <= source_level:
                source_level = None
            if title in {"源码入口", "Source entrypoints"}:
                source_level = level
        elif number < len(lines) and re.match(r"^ {0,3}(?:=+|-+)\s*$", lines[number]) and line.strip():
            flush_paragraph()
            base = slug(line.strip())
            count = occurrences[base]
            occurrences[base] += 1
            doc.anchors.add(base if count == 0 else f"{base}-{count}")
        doc.anchors.update(html.unescape(a) for a in re.findall(r'<a\s+(?:id|name)=[\"\x27]([^\"\x27]+)', line, re.I))
        definition = _REFERENCE.match(line)
        if definition:
            flush_paragraph()
            references[" ".join(definition[1].lower().split())] = definition[2].strip("<>")
            continue
        visible.append((number, line, source_level is not None))
        doc.prose.append(line)
        example = any(_EXAMPLE.search(title) for _, title in headings)
        if not line.strip() or "|" not in line:
            in_table = False
        if table_separator(line) or (number < len(lines) and table_separator(lines[number])):
            in_table = True
        if match or example or in_table or not line.strip() or line.lstrip().startswith(("|", ">")) or re.fullmatch(r"\s*[-=*]{3,}\s*", line):
            flush_paragraph()
        else:
            if re.match(r"^\s*(?:[-+*]|\d+[.)])\s+", line):
                flush_paragraph()
            if not paragraph:
                paragraph_line = number
            paragraph.append(line.strip())
        if not example and (_RESULT.search(line) or _RUN_HEADING.search(line)):
            doc.issues.append(issue("run-record", name, number, "单次执行报告不属于维护文档"))
        if not example and _TEMP_REPORT.search(line) and re.search(r"(?:日志|截图|证据|报告).*(?:位于|保存在)|(?:验证|复验|运行)记录", line):
            doc.issues.append(issue("run-record", name, number, "临时验证产物位置应保留在交付或 CI 中"))
    flush_paragraph()
    for number, line, source in visible:
        # Inline code may contain literal Markdown examples. A link label can itself
        # contain code, so blank it without removing surrounding link syntax.
        prose = re.sub(r"`+([^`\n]*)`+", "", line)
        doc.links.extend(Link(target, number, source) for target in inline_targets(prose))
        for m in re.finditer(r"!?\[([^\]]+)\]\[([^\]]*)\]", prose):
            label = " ".join((m[2] or m[1]).lower().split())
            if label in references:
                doc.links.append(Link(references[label], number, source))
            else:
                doc.issues.append(issue("reference-link", name, number, "引用式链接没有定义"))
        for m in re.finditer(r"(?<!!)\[([^\]]+)\](?![\[(])", prose):
            label = " ".join(m[1].lower().split())
            if label in references:
                doc.links.append(Link(references[label], number, source))
    return doc


def visible_prose(text: str) -> str:
    """Count displayed prose, not destination URLs or markup."""
    text = re.sub(r"!?\[([^\]]*)\]\((?:<[^>]*>|[^)]*)\)", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"<[^>]*>|https?://\S+", "", text)
    text = re.sub(r"^[\s*+\-\d.)]+", "", text)
    return " ".join(html.unescape(text).replace("`", "").split())


def local_target(root: Path, name: str, target: str) -> tuple[str, str] | None:
    url = urlsplit(html.unescape(target))
    if url.scheme or url.netloc:
        # Links to this repository's moving main tree are local documentation links.
        prefix = "/ling-ye/agentstrata/"
        path = unquote(url.path)
        if url.hostname == "github.com" and path.lower().startswith(prefix):
            remainder = path[len(prefix):]
            if remainder.startswith(("blob/main/", "tree/main/")):
                path = remainder.split("/", 2)[2]
            else:
                return None
        else:
            return None
        resolved = root / path
    else:
        path = unquote(url.path)
        resolved = root / path.lstrip("/") if path.startswith("/") else (root / name).parent / path if path else root / name
    resolved = resolved.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("链接超出候选源码范围")
    return resolved.relative_to(root).as_posix(), unquote(url.fragment)


def check(root: Path, changed_paths: tuple[str, ...] | None = None) -> dict:
    root = root.resolve()
    documents, violations, hints = {}, [], []
    for name in discover(root):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            violations.append(issue("document-path", name, 1, "维护文档必须是候选内的普通文件"))
            continue
        try:
            documents[name] = parse(name, path.read_text(encoding="utf-8"))
        except (UnicodeError, OSError):
            violations.append(issue("document-encoding", name, 1, "维护文档必须可读取且为 UTF-8"))
    graph, sources, paragraphs = defaultdict(set), defaultdict(list), defaultdict(set)
    for name, doc in documents.items():
        violations.extend(doc.issues)
        for link in doc.links:
            try:
                target = local_target(root, name, link.target)
            except ValueError:
                violations.append(issue("local-link", name, link.line, "链接超出候选范围或 URL 无效"))
                continue
            if target is None:
                continue
            destination, fragment = target
            path = root / destination
            if not path.exists():
                violations.append(issue("local-link", name, link.line, "本地链接目标不存在", target=destination))
                continue
            navigation = destination + "/README.md" if path.is_dir() else destination
            if navigation in documents:
                graph[name].add(navigation)
                if fragment and fragment not in documents[navigation].anchors:
                    violations.append(issue("anchor", name, link.line, "本地标题或锚点不存在", target=navigation, anchor=fragment))
            elif fragment and re.fullmatch(r"L\d+(?:-L\d+)?", fragment) and path.is_file():
                last = int(fragment.split("L")[-1])
                if last < 1 or last > len(path.read_bytes().splitlines()):
                    violations.append(issue("source-line", name, link.line, "源码行号超出文件范围", target=destination))
            parts = PurePosixPath(destination).parts
            if link.source and parts and (parts[0] in SOURCE_ROOTS or destination in {"pyproject.toml", "uv.lock"}):
                sources[name].append({"path": destination, "line": link.line})
        if name.startswith("docs/reference/") and not sources[name]:
            violations.append(issue("source-entry", name, 1, "领域正文需在“源码入口”中链接实际实现、配置或测试"))
        for text in re.split(r"\n\s*\n", "\n".join(doc.prose)):
            normalized = " ".join(text.split())
            if len(normalized) >= 180 and not normalized.startswith(("#", "|", "- [")):
                paragraphs[normalized].add(name)
        if len(doc.text.splitlines()) > 300:
            hints.append(issue("long-document", name, 1, "正文较长，请按独立任务判断是否拆分；这不是长度门禁"))
        for line, paragraph in doc.paragraphs:
            if len(visible_prose(paragraph)) > 500:
                hints.append(issue("dense-paragraph", name, line, "段落或列表项信息密集，请按独立问题拆分；这不是长度门禁"))
    reached = set()
    queue = deque(name for name in ("README.md", "AGENTS.md") if name in documents)
    while queue:
        name = queue.popleft()
        if name in reached:
            continue
        reached.add(name)
        queue.extend(sorted(graph[name] - reached))
    for name in documents:
        if name not in reached and name != "specs/_template/spec.md":
            violations.append(issue("orphan", name, 1, "维护文档无法从 README 或 AGENTS 的链接进入"))
    for names in paragraphs.values():
        if len(names) > 1:
            first, *others = sorted(names)
            hints.append(issue("possible-duplication", first, 1, "多篇文档包含相同长段落，请核对唯一事实源", related_paths=others))
    for changed in sorted(set(changed_paths or ())):
        p = PurePosixPath(changed)
        if p.is_absolute() or ".." in p.parts or "\\" in changed:
            raise ValueError("变更路径必须为仓库相对路径")
        for name, entries in sources.items():
            matched = [e for e in entries if changed == e["path"] or changed.startswith(e["path"] + "/")]
            if matched:
                hints.append(issue("source-changed", name, matched[0]["line"], "关联源码有变更，请核对描述；不要求无条件修改文档", source_path=changed))
    return {"violations": violations, "review_hints": hints, "documents": sorted(documents), "source_links": dict(sources),
            "change_context": {"known": changed_paths is not None, "paths": sorted(set(changed_paths or ()))}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--changed-path", action="append")
    parser.add_argument("--changes-known", action="store_true", help="caller supplied the complete change set, which may be empty")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        parser.error("candidate root is not a directory")
    try:
        changes = tuple(args.changed_path or ()) if args.changes_known or args.changed_path is not None else None
        result = check(args.root, changes)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result["change_context"]["known"]:
            print(f"SOURCE CHANGES: 已提供 {len(result['change_context']['paths'])} 个变更路径")
        else:
            print("SOURCE CHANGES: 未提供变更范围，仅检查文档结构与正文规则")
        for item in result["violations"]:
            print(f"{item['path']}:{item['line']}: {item['rule']}: {item['message']}")
        for item in result["review_hints"]:
            print(f"REVIEW {item['path']}:{item['line']}: {item['message']}")
        if not result["violations"]:
            print(f"OK: documentation ({len(result['documents'])} documents)")
    return int(bool(result["violations"]))


if __name__ == "__main__":
    raise SystemExit(main())
