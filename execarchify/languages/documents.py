"""Search anchors for markup, configuration and build manifests."""

import json
import re

from ..model import Definition, Facts


_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_RST = re.compile(r"^([=\-~^\"'`:#*+_])\1{2,}\s*$")
_JSON_TOKEN = re.compile(r'["{}\[\],:]')
_SECTION = re.compile(r"^\s*(\[\[?)([^\[\]]{1,256})(\]\]?)\s*(?:[#;].*)?$")
_ASSIGNMENT = re.compile(r"""^\s*(?:"([^"]+)"|'([^']+)'|([\w.-]+))\s*([=:])""")
_YAML_QUOTED = re.compile(r"""^(?:"([^"]{1,256})"|'([^']{1,256})')\s*:(?=\s|$)""")
_YAML_PLAIN = re.compile(r"[\w][\w .-]{0,255}")
_YAML_DELIMITER = re.compile(r":(?=\s|$)")
_MAKE_RULE = re.compile(r"^([A-Za-z_./][\w./%-]*(?: +[A-Za-z_./][\w./%-]*)*)\s*:(?![=])")
_STAGE = re.compile(r"^\s*FROM\s+\S+(?:\s+\S+)*?\s+AS\s+([\w.-]+)\s*(?:#.*)?$", re.IGNORECASE)
_MAX_ANCHORS = 25_000


def _add(facts: Facts, name: str, qualified: str, kind: str, line: int, end: int | None = None) -> None:
    if name and len(qualified) <= 1024 and len(facts.definitions) < _MAX_ANCHORS:
        facts.definitions.append(Definition(name, qualified, kind, line, end or line, "heuristic"))


def _headings(text: str, facts: Facts) -> None:
    lines = text.splitlines()
    opened: list[tuple[int, str, str, int]] = []
    styles: dict[str, int] = {}
    fence = ""
    comment = False
    previous = ""

    def finish(before: int, level: int) -> None:
        while opened and opened[-1][0] >= level:
            _, name, qualified, start = opened.pop()
            _add(facts, name, qualified, "heading", start, max(start, before))

    for number, line in enumerate(lines, start=1):
        level, title, start = 0, "", number
        if facts.language == "markdown":
            marker = _FENCE.match(line)
            if fence:
                if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                    fence = ""
                previous = ""
                continue
            if marker:
                fence = marker[1]
                previous = ""
                continue
            if comment or line.lstrip().startswith("<!--"):
                comment = "-->" not in line
                previous = ""
                continue
            if match := _ATX.match(line):
                level = len(match[1])
                title = re.sub(r"[ \t]+#+[ \t]*$", "", match[2] or "").strip()
            elif previous and (match := _SETEXT.match(line)):
                level = 1 if match[1][0] == "=" else 2
                title, start = previous, number - 1
        elif previous and (match := _RST.match(line)) and len(line.strip()) >= len(previous):
            level = styles.setdefault(match[1], len(styles) + 1)
            title, start = previous, number - 1
        if title and len(title) <= 256:
            finish(start - 1, level)
            qualified = f"{opened[-1][2]}.{title}" if opened else title
            if len(qualified) <= 1024:
                opened.append((level, title, qualified, start))
            previous = ""
        else:
            previous = line.strip() if len(line) <= 512 and not line.startswith(("    ", "\t")) else ""
    finish(len(lines), 0)


def _json_keys(text: str, facts: Facts) -> None:
    owner = ""
    pending = ""
    stack = []
    position = 0
    line = 1
    while match := _JSON_TOKEN.search(text, position):
        if len(facts.definitions) >= _MAX_ANCHORS:
            break
        line += text.count("\n", position, match.start())
        position = match.end()
        value = match[0]
        if value in {"{", "["}:
            if len(stack) >= 128:
                return
            stack.append(owner)
            owner = pending or owner
            pending = ""
        elif value in {"}", "]"}:
            owner = stack.pop() if stack else ""
            pending = ""
        elif value == ",":
            pending = ""
        elif value == '"':
            try:
                key, position = json.decoder.scanstring(text, match.end(), True)
            except ValueError:
                return
            if len(key) > 256:
                continue
            following = position
            while following < len(text) and text[following].isspace():
                following += 1
            if following < len(text) and text[following] == ":":
                # Surrogates are possible escape sequences in JSON but cannot
                # be stored as UTF-8 text in the index.
                if not key or "\0" in key or any(0xD800 <= ord(char) <= 0xDFFF for char in key):
                    continue
                pending = f"{owner}.{key}" if owner else key
                if len(pending) > 1024:
                    pending = ""
                else:
                    _add(facts, key, pending, "key", line)


def _config(text: str, facts: Facts) -> None:
    section = ""
    yaml_parents: list[tuple[int, str]] = []
    scalar_indent = -1
    quote = ""
    for line_number, line in enumerate(text.splitlines(), start=1):
        if len(facts.definitions) >= _MAX_ANCHORS:
            return
        if len(line) > 8192:
            continue
        if facts.language == "yaml":
            indent = len(line) - len(line.lstrip(" "))
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if scalar_indent >= 0 and indent > scalar_indent:
                continue
            scalar_indent = -1
            if line.strip() in {"---", "..."}:
                yaml_parents.clear()
                continue
            entry = line[indent:]
            if entry.startswith("- "):
                entry = entry[2:].lstrip()
            if entry.startswith(("'", '"')):
                marker = _YAML_QUOTED.match(entry)
                if not marker:
                    continue
                key = marker[1] or marker[2]
            else:
                marker = _YAML_DELIMITER.search(entry)
                if not marker:
                    continue
                key = entry[:marker.start()].rstrip()
                if not _YAML_PLAIN.fullmatch(key):
                    continue
            while yaml_parents and yaml_parents[-1][0] >= indent:
                yaml_parents.pop()
            prefix = yaml_parents[-1][1] if yaml_parents else ""
            qualified = f"{prefix}.{key}" if prefix else key
            _add(facts, key, qualified, "key", line_number)
            value = entry[marker.end():].strip()
            if not value or value.startswith("#"):
                if len(qualified) <= 1024:
                    yaml_parents.append((indent, qualified))
            elif value.startswith(("|", ">")):
                scalar_indent = indent
            continue
        if quote:
            if quote in line:
                quote = ""
            continue
        if match := _SECTION.match(line):
            if len(match[1]) == len(match[3]):
                section = match[2].strip()
                _add(facts, section, section, "section", line_number)
            continue
        if match := _ASSIGNMENT.match(line):
            if facts.language == "toml" and match[4] != "=":
                continue
            key = match[1] or match[2] or match[3]
            qualified = f"{section}.{key}" if section else key
            _add(facts, key, qualified, "key", line_number)
            if facts.language == "toml":
                value = line[match.end():].lstrip()
                marker = value[:3]
                if marker in {"'''", '"""'} and marker not in value[3:]:
                    quote = marker


def extract_document(text: str, facts: Facts) -> None:
    if facts.language in {"markdown", "rst"}:
        _headings(text, facts)
    elif facts.language == "json":
        _json_keys(text, facts)
    elif facts.language in {"yaml", "toml", "ini", "properties"}:
        _config(text, facts)
    elif facts.language in {"make", "dockerfile"}:
        for number, line in enumerate(text.splitlines(), start=1):
            if facts.language == "make" and (match := _MAKE_RULE.match(line)):
                for target in match[1].split():
                    _add(facts, target, target, "target", number)
            elif facts.language == "dockerfile" and (match := _STAGE.match(line)):
                _add(facts, match[1], match[1], "stage", number)
