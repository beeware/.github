"""Shared helpers for editing the jinja-templated dict entries in
`{{ cookiecutter.format }}/briefcase.toml`.

That file isn't valid TOML on its own -- it's a jinja template that, for each
Python version tag, picks a `"key = value"` string out of a literal dict and
splices it into the surrounding TOML. e.g.:

    {{ {
        "3.11": 'support_revision = "10"',
        "3.12": 'support_revision = "10"',
    }.get(cookiecutter.python_version|py_tag, "") }}

All four Briefcase templates (macOS, iOS, Windows, Linux-flatpak) now use
exactly one normalized entry format: the outer dict-value string is
single-quoted, and the inner `key = value` is always double-quoted, even for
bare numeric revisions (e.g. `'support_revision = "10"'`). These helpers only
read and write that one format -- there is no support for older/legacy
variants (unquoted numeric values, single-quoted inner values, etc.).

These helpers let update_support.py / update_stub.py find and rewrite those
dict entries, plus the odd scalar entry (e.g. `stub_binary_revision`),
without needing a real jinja/TOML parser.
"""

from __future__ import annotations

import re
from pathlib import Path

TAG_PATTERN = r"\d+\.\d+"

# Matches a jinja `{% if cookiecutter.<var> == "<value>" %}` line (with an
# optional leading `-` before `%}`), e.g. `{% if cookiecutter.host_arch ==
# "AMD64" -%}`.
EQ_IF_RE = re.compile(r'{%-?\s*if\s+cookiecutter\.(\w+)\s*==\s*"([^"]+)"\s*-?%}')

# Matches a jinja `{% if cookiecutter.<var> %}` / `{% if not
# cookiecutter.<var> %}` boolean-flag line. Note this only matches when
# nothing else appears between the variable name and the closing `%}`, so it
# never accidentally matches an EQ_IF_RE line.
BOOL_IF_RE = re.compile(r"{%-?\s*if\s+(not\s+)?cookiecutter\.(\w+)\s*-?%}")

ELSE_RE = re.compile(r"{%-?\s*else\s*-?%}")
ENDIF_RE = re.compile(r"{%-?\s*endif\s*-?%}")


def read_toml(path: Path) -> str:
    return Path(path).read_text()


def write_toml(path: Path, text: str) -> None:
    Path(path).write_text(text)


def entry_regex(key: str) -> re.Pattern[str]:
    """Build a regex matching one normalized `"<tag>": 'key = "value"',` dict
    entry line for `key`."""
    return re.compile(
        rf'^(?P<indent>\s*)"(?P<tag>{TAG_PATTERN})":\s*'
        rf"'{re.escape(key)} = \"(?P<value>[^\"]*)\"',\s*$"
    )


def render_entry(indent: str, tag: str, key: str, value: str) -> str:
    """Render one normalized dict entry line."""
    return f'{indent}"{tag}": \'{key} = "{value}"\',\n'


def tags_present(lines: list[str], *patterns: re.Pattern[str]) -> set[str]:
    """Return the set of Python version tags with an entry matching any of
    `patterns`, anywhere in `lines`."""
    tags: set[str] = set()
    for line in lines:
        for pattern in patterns:
            match = pattern.match(line)
            if match:
                tags.add(match.group("tag"))
    return tags


def apply_updates(
    lines: list[str],
    pattern: re.Pattern[str],
    key: str,
    values: dict[str, str | None],
    line_range: range | None = None,
) -> set[int]:
    """Update every line matching `pattern` whose tag is a key in `values`.

    `lines` is mutated in place. `values[tag]` of None means: this entry
    should be removed entirely (e.g. no matching release asset was found for
    that tag). The set of line indices that should be dropped is returned;
    the caller is responsible for actually removing them (via `render`), so
    that indices computed by other callers/passes stay valid.

    If `line_range` is given, only lines within it are considered -- this is
    needed when the same `pattern`/`key` occurs in more than one place in the
    file (e.g. per-architecture hash dicts).
    """
    to_delete: set[int] = set()
    indices = line_range if line_range is not None else range(len(lines))
    for i in indices:
        match = pattern.match(lines[i])
        if not match or match.group("tag") not in values:
            continue
        tag = match.group("tag")
        value = values[tag]
        if value is None:
            to_delete.add(i)
            continue
        lines[i] = render_entry(match.group("indent"), tag, key, value)
    return to_delete


def render(lines: list[str], to_delete: set[int]) -> str:
    return "".join(line for i, line in enumerate(lines) if i not in to_delete)


def update_scalar(text: str, key: str, value: str) -> str:
    """Update a plain (non-per-tag) `key = "value"` scalar entry, e.g.
    `stub_binary_revision = "16"`. Raises ValueError if `key` isn't found."""
    pattern = re.compile(rf'({re.escape(key)} = ")[^"]*(")')
    new_text, count = pattern.subn(rf"\g<1>{value}\g<2>", text, count=1)
    if count == 0:
        raise ValueError(f"Could not find {key} in briefcase.toml")
    return new_text


def scalar_present(text: str, key: str) -> bool:
    """True if a plain `key = "value"` scalar entry is present anywhere in
    `text`."""
    return re.search(rf'{re.escape(key)} = "[^"]*"', text) is not None


def find_conditional_blocks(
    lines: list[str],
    *flags: str,
    arch_alternatives: dict[str, dict[str, str]] | None = None,
) -> dict[tuple, tuple[int, int]]:
    """Walk the jinja `{% if %}` / `{% else %}` / `{% endif %}` structure of
    `lines`, and find the (start, end) line range (inclusive, 0-indexed) of
    every line that falls under a particular combination of `flags` values.

    `flags` names the `cookiecutter.<flag>` variables of interest. Each may
    be either a boolean flag (`{% if cookiecutter.x %}` / `{% if not
    cookiecutter.x %}`) or a string-equality flag (`{% if cookiecutter.x ==
    "VALUE" %}`) -- both are tracked automatically as the file is walked.

    For boolean flags, the `{% else %}` branch's value is simply the
    logical negation. String-equality flags have no such generic inverse, so
    `arch_alternatives` must supply one: `{flag_name: {value: other_value}}`,
    e.g. `{"host_arch": {"AMD64": "ARM64", "ARM64": "AMD64"}}`.

    The returned dict is keyed by a tuple of values in the same order as
    `flags`; only combinations that actually appear as *leaf* branches (i.e.
    every requested flag has a known value in that branch) are included.
    """
    arch_alternatives = arch_alternatives or {}
    stack: list[list] = []  # each entry: [var_name, current_value]
    blocks: dict[tuple, tuple[int, int]] = {}

    def current_state() -> dict[str, object]:
        state: dict[str, object] = {}
        for var_name, value in stack:
            state[var_name] = value
        return state

    for i, line in enumerate(lines):
        eq_match = EQ_IF_RE.search(line)
        if eq_match:
            stack.append([eq_match.group(1), eq_match.group(2)])
            continue

        bool_match = BOOL_IF_RE.search(line)
        if bool_match:
            negate = bool(bool_match.group(1))
            stack.append([bool_match.group(2), not negate])
            continue

        if ELSE_RE.search(line):
            if stack:
                var_name, value = stack[-1]
                if isinstance(value, bool):
                    stack[-1][1] = not value
                else:
                    alternatives = arch_alternatives.get(var_name, {})
                    if value not in alternatives:
                        raise ValueError(
                            f"No alternative value known for "
                            f"{var_name}={value!r}; pass it via "
                            "arch_alternatives"
                        )
                    stack[-1][1] = alternatives[value]
            continue

        if ENDIF_RE.search(line):
            if stack:
                stack.pop()
            continue

        state = current_state()
        if not all(flag in state for flag in flags):
            continue

        key = tuple(state[flag] for flag in flags)
        if key not in blocks:
            blocks[key] = (i, i)
        else:
            start, _ = blocks[key]
            blocks[key] = (start, i)

    return blocks
