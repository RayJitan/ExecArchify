"""A bounded lexer for the heuristic brace-language adapters."""

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class Token:
    value: str
    line: int
    end_line: int
    kind: str


TOKEN_LIMIT = 120_000
_PIECE = re.compile(r"""
    (?P<space>\s+)
  | (?P<line_comment>//[^\n]*)
  | (?P<block_comment>/\*)
  | (?P<cpp_raw>(?:u8|u|U|L)?R"(?P<delimiter>[^\s()\\]{0,16})\()
  | (?P<rust_raw>(?:br|r)(?P<hashes>\#{0,64})")
  | (?P<verbatim>\$?@"|@\$") | (?P<triple>"{3,})
  | (?P<quote>['"`])
  | (?P<word>(?:[^\W\d]|\$)[\w$]*)
  | (?P<number>\d[\w.]*)
  | (?P<punct>\?\.|::|->|=>|\.\.\.|&&|\|\||==|!=|<=|>=|\+\+|--|\S)
""", re.VERBOSE)

_QUOTE_MARKS = {q: re.compile(f"[{re.escape(q)}\\\\\n]") for q in ('"', "'", "`")}
_LIFETIME = re.compile(r"'[^\W\d]\w*")
_BLOCK_MARKS = re.compile(r"/\*|\*/")
_REGEX_FLAGS = re.compile(r"[a-zA-Z]*")
_REGEX_START = {"", "(", "[", "{", "=", ":", ",", ";", "!", "?", "=>", "return",
                "case", "throw", "typeof", "void", "yield", "await", "&&", "||"}


def _string_end(text: str, start: int, quote: str, multiline: bool) -> int:
    for_mark = _QUOTE_MARKS[quote].search
    position = start
    while match := for_mark(text, position):
        char = match[0]
        if char == quote:
            return match.end()
        if char == "\n" and not multiline:
            return match.start()
        position = match.end() + (char == "\\")
    return len(text)


def _verbatim_end(text: str, start: int) -> int:
    position = start
    while (quote := text.find('"', position)) >= 0:
        position = quote + 1
        if position == len(text) or text[position] != '"':
            return position
        position += 1
    return len(text)


def _comment_end(text: str, start: int, nested: bool) -> int:
    if not nested:
        end = text.find("*/", start)
        return len(text) if end < 0 else end + 2
    depth = 1
    for match in _BLOCK_MARKS.finditer(text, start):
        depth += 1 if match[0] == "/*" else -1
        if depth == 0:
            return match.end()
    return len(text)


def _regex_end(text: str, start: int) -> int:
    position = start
    in_class = False
    while position < len(text) and text[position] != "\n":
        char = text[position]
        if char == "\\":
            position += 2
            continue
        if char == "/" and not in_class:
            return _REGEX_FLAGS.match(text, position + 1).end()
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        position += 1
    # An unterminated literal consumes this line. Retrying from every slash
    # in an unclosed character class would rescan the same text repeatedly.
    return min(position, len(text))


def _directive_end(text: str, start: int) -> int:
    position = start
    while (end := text.find("\n", position)) >= 0:
        if end == position or text[end - 1] != "\\":
            return end
        position = end + 1
    return len(text)


def tokenize(text: str, language: str) -> list[Token]:
    tokens = []
    line = 1
    position = 0
    is_js = language in {"javascript", "typescript"}
    while position < len(text):
        match = _PIECE.match(text, position)
        group = match.lastgroup
        value = match[0]
        end = match.end()
        kind = group
        if group == "block_comment":
            end = _comment_end(text, end, language == "rust")
        elif group in {"cpp_raw", "rust_raw", "triple"}:
            if group == "cpp_raw":
                terminator = ")" + match["delimiter"] + '"'
            elif group == "rust_raw":
                terminator = '"' + match["hashes"]
            else:
                terminator = value
            found = text.find(terminator, end)
            end = len(text) if found < 0 else found + len(terminator)
            value, kind = "", "string"
        elif group == "verbatim":
            end = _verbatim_end(text, end)
            value, kind = "", "string"
        elif group == "quote":
            lifetime = _LIFETIME.match(text, position) if value == "'" and language == "rust" else None
            if lifetime and text[lifetime.end():lifetime.end() + 1] != "'":
                end, value, kind = lifetime.end(), lifetime[0], "lifetime"
            else:
                end = _string_end(text, end, value, value == "`" or language == "rust")
                closed = end > match.end() and text[end - 1] == value
                value = text[match.end():end - 1] if closed and end - position <= 1024 else ""
                kind = "string"
        elif value == "#" and language in {"c", "cpp"} and (not tokens or tokens[-1].end_line < line):
            end = _directive_end(text, end)
            value, kind = text[position:min(end, position + 2048)], "directive"
        elif value == "/" and is_js and (not tokens or tokens[-1].value in _REGEX_START):
            end, value, kind = _regex_end(text, end), "", "literal"
        end_line = line + text.count("\n", position, end)
        if group not in {"space", "line_comment", "block_comment"}:
            if len(tokens) >= TOKEN_LIMIT:
                return []
            tokens.append(Token(value, line, end_line, kind))
        position, line = end, end_line
    return tokens


def delimiter_pairs(tokens: list[Token]) -> dict[int, int]:
    stack = []
    pairs = {}
    opening = {")": "(", "]": "[", "}": "{"}
    for index, token in enumerate(tokens):
        if token.kind != "punct":
            continue
        if token.value in {"(", "[", "{"}:
            stack.append((token.value, index))
        elif token.value in opening:
            if not stack or stack[-1][0] != opening[token.value]:
                stack.clear()
                continue
            _, start = stack.pop()
            pairs[start] = index
            pairs[index] = start
    return pairs
