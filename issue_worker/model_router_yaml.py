"""A tiny, dependency-free loader for the YAML subset ``model_router`` uses.

Every other module in ``issue_worker/`` sticks to the standard library (see
``dynamic_router.py``'s own JSON-only config handling), and the app ships a
user's own ``python3`` with no install step — there is nowhere to depend on
PyYAML from. ``skills/model-router/models.yaml`` and ``routing-rules.yaml``
are written and read only by this project, so instead of a real YAML grammar
this implements exactly the subset those two files use: block mappings,
block sequences (including sequences of mappings), quoted or bare scalar
strings, ints, floats, booleans, null, ``#`` comments, and flow-style
``[a, b, c]`` lists of bare/quoted scalars for compact fields like
``strengths``. No anchors, multi-document streams, or block scalars.

Callers should treat a parse failure as "the file is malformed" (``ValueError``)
and fall back to the module's built-in defaults, the same resilience
``dynamic_router.load_routing_tiers`` already applies to its own config.
"""

from __future__ import annotations

import re
from typing import Any


class YamlError(ValueError):
    """The input is not valid within this loader's supported subset."""


def load(text: str) -> Any:
    lines = _strip_comments_and_blanks(text)
    if not lines:
        return None
    value, consumed = _parse_block(lines, 0, 0)
    if consumed != len(lines):
        raise YamlError(f"unexpected content at line {lines[consumed][0] + 1}")
    return value


def _strip_comments_and_blanks(text: str) -> list[tuple[int, int, str]]:
    """(original line number, indent, content) for every non-blank, non-comment line."""
    rows: list[tuple[int, int, str]] = []
    for number, raw in enumerate(text.splitlines()):
        stripped = _strip_inline_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if "\t" in stripped[:indent]:
            raise YamlError(f"tabs are not supported for indentation (line {number + 1})")
        rows.append((number, indent, stripped.strip()))
    return rows


def _strip_inline_comment(line: str) -> str:
    """Drop a trailing ``# ...`` comment, respecting quotes."""
    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            if index == 0 or line[index - 1] in (" ", "\t"):
                return line[:index]
    return line


def _parse_block(
    lines: list[tuple[int, int, str]], start: int, indent: int
) -> tuple[Any, int]:
    if start >= len(lines):
        raise YamlError("unexpected end of input")
    _, first_indent, first_content = lines[start]
    if first_indent < indent:
        raise YamlError(f"unexpected indentation at line {lines[start][0] + 1}")
    if first_content.startswith("- ") or first_content == "-":
        return _parse_sequence(lines, start, first_indent)
    return _parse_mapping(lines, start, first_indent)


def _parse_sequence(
    lines: list[tuple[int, int, str]], start: int, indent: int
) -> tuple[list[Any], int]:
    items: list[Any] = []
    index = start
    while index < len(lines):
        number, line_indent, content = lines[index]
        if line_indent != indent or not (content == "-" or content.startswith("- ")):
            break
        remainder = "" if content == "-" else content[2:]
        if not remainder.strip():
            index += 1
            if index < len(lines) and lines[index][1] > indent:
                value, index = _parse_block(lines, index, lines[index][1])
            else:
                value = None
            items.append(value)
            continue
        if ":" in remainder and _looks_like_mapping_entry(remainder):
            # A mapping that starts inline with the dash, e.g. "- key: value".
            item_lines = [(number, indent + 2, remainder)]
            index += 1
            while index < len(lines) and lines[index][1] > indent:
                item_lines.append(lines[index])
                index += 1
            value, consumed = _parse_mapping(item_lines, 0, indent + 2)
            if consumed != len(item_lines):
                raise YamlError(f"malformed list entry near line {number + 1}")
            items.append(value)
        else:
            items.append(_parse_scalar(remainder))
            index += 1
    return items, index


def _looks_like_mapping_entry(text: str) -> bool:
    key, sep, _ = _split_key_value(text)
    return sep and bool(re.fullmatch(r"[A-Za-z0-9_.\-]+", key.strip()))


def _parse_mapping(
    lines: list[tuple[int, int, str]], start: int, indent: int
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    index = start
    while index < len(lines):
        number, line_indent, content = lines[index]
        if line_indent != indent:
            break
        if content.startswith("- "):
            break
        key, sep, inline_value = _split_key_value(content)
        if not sep:
            raise YamlError(f"expected 'key: value' at line {number + 1}")
        key = _parse_key(key)
        index += 1
        if inline_value.strip():
            result[key] = _parse_scalar(inline_value.strip())
            continue
        if index < len(lines) and lines[index][1] > indent:
            value, index = _parse_block(lines, index, lines[index][1])
            result[key] = value
        else:
            result[key] = None
    return result, index


def _split_key_value(content: str) -> tuple[str, bool, str]:
    """Split ``key: value`` respecting quoted keys/values containing ':'."""
    in_single = False
    in_double = False
    for index, char in enumerate(content):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == ":" and not in_single and not in_double:
            if index + 1 == len(content) or content[index + 1] in (" ", "\t"):
                return content[:index], True, content[index + 1 :]
    return content, False, ""


def _parse_key(raw: str) -> str:
    key = raw.strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in ("'", '"'):
        return key[1:-1]
    return key


def _parse_scalar(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        return _parse_flow_sequence(text[1:-1])
    if text.startswith("{") and text.endswith("}"):
        return _parse_flow_mapping(text[1:-1])
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("null", "~", ""):
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def _split_flow_items(inner: str) -> list[str]:
    """Top-level comma-separated items of a flow ``[...]``/``{...}`` body."""
    inner = inner.strip()
    if not inner:
        return []
    items: list[str] = []
    depth = 0
    current = ""
    in_single = False
    in_double = False
    for char in inner:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        if char == "," and not in_single and not in_double and depth == 0:
            items.append(current)
            current = ""
            continue
        if char in "[{" and not in_single and not in_double:
            depth += 1
        elif char in "]}" and not in_single and not in_double:
            depth -= 1
        current += char
    items.append(current)
    return [item.strip() for item in items if item.strip()]


def _parse_flow_sequence(inner: str) -> list[Any]:
    return [_parse_scalar(item) for item in _split_flow_items(inner)]


def _parse_flow_mapping(inner: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in _split_flow_items(inner):
        key, sep, value = _split_key_value(item)
        if not sep:
            raise YamlError(f"malformed flow mapping entry {item!r}")
        result[_parse_key(key)] = _parse_scalar(value.strip())
    return result
