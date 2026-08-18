"""Runtime environment helpers."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from chatcopilot.project import ENV_PREFIX


BOT_SPEC_ENV = f"{ENV_PREFIX}_BOT_SPEC"
_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:\\")


def get_bot_spec_env() -> Path | None:
    """Return the BotSpec path exported for the current process, if any."""

    raw = os.environ.get(BOT_SPEC_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def set_bot_spec_env(path: Path) -> None:
    """Expose the selected BotSpec path to child/runtime modules."""

    os.environ[BOT_SPEC_ENV] = str(path.expanduser().resolve())


def expand_leading_home(value: str, *, home: Path | None = None) -> str:
    """Expand only an explicit leading home marker, never arbitrary shell syntax."""

    resolved_home = str((home or Path.home()).expanduser())
    for marker in ("~", "$HOME", "${HOME}"):
        if value == marker:
            return resolved_home
        if value.startswith(f"{marker}/"):
            return resolved_home + value[len(marker) :]
    return value


def load_local_env_values(
    path: Path,
    *,
    missing_ok: bool = False,
    expand_home: bool = False,
) -> dict[str, str]:
    """Parse shell-style exports without executing commands or expanding variables."""

    if not path.is_file():
        if missing_ok:
            return {}
        raise FileNotFoundError(f"本地配置不存在：{path}")

    values: dict[str, str] = {}
    for lineno, raw in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(),
        1,
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            lexer = shlex.shlex(_protect_windows_path_values(line), posix=True)
            lexer.whitespace_split = True
            lexer.commenters = "#"
            parts = list(lexer)
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno} 不是合法的 shell export 行") from exc
        assignments = parts[1:] if parts and parts[0] == "export" else parts
        for item in assignments:
            if "=" not in item:
                raise ValueError(f"{path}:{lineno} 缺少 KEY=value")
            key, value = item.split("=", 1)
            if not key or not (key[0].isalpha() or key[0] == "_") or not all(
                ch.isalnum() or ch == "_" for ch in key
            ):
                raise ValueError(f"{path}:{lineno} 非法 env key")
            values[key] = expand_leading_home(value) if expand_home else value
    return values


def _protect_windows_path_values(line: str) -> str:
    """Keep POSIX shell escapes while preserving bare Windows path separators.

    ``shlex`` correctly handles quotes, comments, and escaped shell characters,
    but a bare ``C:\\...`` or ``\\\\server\\...`` value loses its path
    separators because POSIX mode treats every backslash as an escape. Protect
    only assignment values that begin with a Windows drive or UNC root; all
    other values retain the established shell-style semantics.
    """

    output: list[str] = []
    cursor = 0
    at_word_start = True
    while cursor < len(line):
        character = line[cursor]
        if character.isspace():
            output.append(character)
            cursor += 1
            at_word_start = True
            continue
        if character == "#" and at_word_start:
            output.append(line[cursor:])
            break
        if not at_word_start:
            output.append(character)
            cursor += 1
            continue

        export_match = re.match(r"export(?=\s)", line[cursor:])
        if export_match is not None:
            end = cursor + export_match.end()
            output.append(line[cursor:end])
            cursor = end
            at_word_start = False
            continue

        assignment = _ENV_ASSIGNMENT_RE.match(line, cursor)
        if assignment is None:
            end = _shell_word_end(line, cursor)
            output.append(line[cursor:end])
            cursor = end
            at_word_start = False
            continue

        output.append(line[cursor : assignment.end()])
        value_start = assignment.end()
        value_end = _shell_word_end(line, value_start)
        output.append(_protect_windows_path_value(line[value_start:value_end]))
        cursor = value_end
        at_word_start = False
    return "".join(output)


def _shell_word_end(line: str, start: int) -> int:
    quote = ""
    cursor = start
    while cursor < len(line):
        character = line[cursor]
        if quote == "'":
            if character == "'":
                quote = ""
            cursor += 1
            continue
        if quote == '"':
            if character == "\\" and cursor + 1 < len(line):
                cursor += 2
                continue
            if character == '"':
                quote = ""
            cursor += 1
            continue
        if character in {"'", '"'}:
            quote = character
            cursor += 1
            continue
        if character == "\\" and cursor + 1 < len(line):
            cursor += 2
            continue
        if character.isspace() or character == "#":
            break
        cursor += 1
    return cursor


def _protect_windows_path_value(value: str) -> str:
    if not value:
        return value
    if value.startswith("'"):
        return value
    if value.startswith('"'):
        # POSIX shlex already preserves ordinary backslashes inside double
        # quotes. Only an unescaped leading UNC pair needs protection; a run
        # of four already uses the established shell-escaped representation
        # and must be left for shlex to reduce to two.
        unc_end = 1
        while unc_end < len(value) and value[unc_end] == "\\":
            unc_end += 1
        protected = '"\\\\\\\\' + value[unc_end:] if unc_end == 3 else value
        quoted_body = value[1:]
        quoted_windows = bool(
            _WINDOWS_DRIVE_RE.match(quoted_body) or quoted_body.startswith("\\\\")
        )
        if quoted_windows and protected.endswith('\\"') and not protected.endswith('\\\\"'):
            protected = protected[:-2] + '\\\\"'
        return protected
    if not (_WINDOWS_DRIVE_RE.match(value) or value.startswith("\\\\")):
        return value

    protected: list[str] = []
    cursor = 0
    if value.startswith("\\\\"):
        unc_end = 2
        while unc_end < len(value) and value[unc_end] == "\\":
            unc_end += 1
        protected.append("\\\\\\\\" if unc_end == 2 else value[:unc_end])
        cursor = unc_end
    while cursor < len(value):
        character = value[cursor]
        if character != "\\":
            protected.append(character)
            cursor += 1
            continue

        run_end = cursor + 1
        while run_end < len(value) and value[run_end] == "\\":
            run_end += 1
        run_length = run_end - cursor
        if run_length > 1:
            # Explicit shell escaping such as ``C:\\\\dir`` keeps its prior
            # meaning; shlex will reduce each pair to one literal separator.
            protected.append(value[cursor:run_end])
        else:
            following = value[run_end] if run_end < len(value) else ""
            if not following:
                protected.append("\\\\")
            elif not (following.isspace() or following in "#'\"\\"):
                protected.append("\\\\")
            else:
                protected.append("\\")
        cursor = run_end
    return "".join(protected)
