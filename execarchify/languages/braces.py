"""Token-based source adapters. Every occurrence from this module is heuristic."""

from dataclasses import dataclass, field
import re

from ..model import Definition, Facts, Reference
from .tokens import Token, delimiter_pairs, tokenize


_JS = {"javascript", "typescript"}
_C_FAMILY = {"c", "cpp", "java", "csharp"}
_PREFIX_PARAMETERS = _JS | {"go", "rust"}
_TYPED_LANGUAGES = _C_FAMILY | {"go"}
_TYPE_KEYWORDS = {
    "javascript": {"class"},
    "typescript": {"class", "interface", "enum", "namespace", "type"},
    "go": {"type"},
    "rust": {"struct", "enum", "trait", "union", "mod", "type"},
    "c": {"struct", "union", "enum"},
    "cpp": {"struct", "class", "union", "enum", "namespace"},
    "java": {"class", "record", "interface", "enum"},
    "csharp": {"class", "record", "struct", "interface", "enum", "namespace"},
}
_NON_CALL = set("""
    if else while for foreach do switch case catch try finally return throw throws
    new delete typeof nameof sizeof alignof alignas decltype instanceof as is in
    fun func function fn class struct record interface enum union trait impl mod
    type namespace package var let const static default using import export from
    match loop select defer go unsafe extern async await yield checked unchecked
    lock fixed synchronized assert with when where requires noexcept operator
    static_cast reinterpret_cast dynamic_cast const_cast _Static_assert
""".split())
_PRIMITIVES = set("void char bool boolean byte sbyte short int long float double string wchar_t".split())
_C_TAIL = {"{", ";", "const", "volatile", "override", "final", "noexcept", "throws",
           "where", "requires", "->", "=>", ":", "&", "&&", "[", "="}
_END_EXPR = {";", ",", "}", ")", "]"}
_ACCESS = {".", "?.", "->", "::"}
_SCAN_LIMIT = 256
_MACRO = re.compile(r"#\s*define\s+([^\W\d]\w*)")
_INCLUDE = re.compile(r'#\s*include\s*[<"]([^>"]+)[>"]')


@dataclass(slots=True)
class _Scope:
    qualified: str
    kind: str
    end: int
    syntax: str
    dynamic: set[str] = field(default_factory=set)


class _Adapter:
    def __init__(self, tokens: list[Token], facts: Facts):
        self.tokens = tokens
        self.pairs = delimiter_pairs(tokens)
        self.facts = facts
        self.language = facts.language
        self.scopes: list[_Scope] = []

    @property
    def owner(self) -> str:
        return self.scopes[-1].qualified if self.scopes else ""

    @property
    def scope_kind(self) -> str:
        return self.scopes[-1].kind if self.scopes else "module"

    def val(self, index: int) -> str:
        if 0 <= index < len(self.tokens):
            token = self.tokens[index]
            if token.kind in {"word", "punct", "number"}:
                return token.value
        return ""

    def word(self, index: int) -> str:
        if 0 <= index < len(self.tokens) and self.tokens[index].kind == "word":
            return self.tokens[index].value
        return ""

    def generic_end(self, start: int) -> int | None:
        depth = 0
        for index in range(start, min(len(self.tokens), start + _SCAN_LIMIT)):
            value = self.val(index)
            if value == "<":
                depth += 1
            elif value == ">":
                depth -= 1
                if depth == 0:
                    return index
            elif value in {";", "=>"}:
                return None
        return None

    def parameters(self, index: int) -> int | None:
        """Opening parenthesis following a name, including common generic forms."""
        cursor = index + 1
        if self.val(cursor) == "::" and self.val(cursor + 1) == "<":
            cursor += 1
        if self.val(cursor) == "<":
            end = self.generic_end(cursor)
            if end is None:
                return None
            cursor = end + 1
        elif self.language == "go" and self.val(cursor) == "[" and cursor in self.pairs:
            cursor = self.pairs[cursor] + 1
        if self.language in _JS and self.val(cursor) == "?.":
            cursor += 1
        if self.language == "typescript" and self.val(cursor) == "?" and self.scope_kind == "type":
            cursor += 1
        return cursor if self.val(cursor) == "(" and cursor in self.pairs else None

    def parameter_names(self, params: int) -> set[str]:
        """Simple bound names, sufficient to prevent a callback-name coincidence."""
        names = set()
        cursor = params + 1
        close = self.pairs[params]
        while cursor < close and cursor - params <= _SCAN_LIMIT:
            boundary = self.seek(cursor, {",", ")"})
            if boundary is None:
                break
            words = []
            index = cursor
            while index < boundary:
                value = self.val(index)
                if value == "=" or (value == ":" and self.language in {"javascript", "typescript", "rust"}):
                    break
                if value in {"(", "[", "{"} and index in self.pairs:
                    if value == "(" and self.val(index + 1) in {"*", "&"} and self.word(index + 2):
                        words.append(self.word(index + 2))
                    index = self.pairs[index]
                elif value == "<" and (end := self.generic_end(index)) is not None:
                    index = end
                elif self.word(index) and value not in {"mut", "ref", "public", "private", "readonly", "protected"}:
                    words.append(value)
                index += 1
            if words:
                if self.language in _PREFIX_PARAMETERS:
                    names.add(words[0])
                elif len(words) > 1:
                    names.add(words[-1])
            cursor = boundary + 1
        return names

    def seek(self, start: int, stops: set[str]) -> int | None:
        """Find a top-level delimiter in a short declaration or import header."""
        cursor = start
        for _ in range(_SCAN_LIMIT):
            if cursor >= len(self.tokens):
                break
            value = self.val(cursor)
            if value in stops:
                return cursor
            if value in {")", "]", "}"}:
                break
            if value in {"(", "[", "{"}:
                if cursor not in self.pairs:
                    break
                cursor = self.pairs[cursor]
            elif value == "<":
                end = self.generic_end(cursor)
                if end is not None:
                    cursor = end
            cursor += 1
        return None

    def expression_end(self, start: int, *, type_expression: bool = False) -> int:
        cursor = start
        previous = start
        for _ in range(_SCAN_LIMIT):
            if cursor >= len(self.tokens) or self.val(cursor) in _END_EXPR:
                break
            value = self.val(cursor)
            if cursor > start and self.tokens[cursor].line > self.tokens[previous].end_line:
                # Automatic semicolon insertion bounds an expression-bodied JS
                # function when a new statement begins on the following line.
                last = self.val(previous)
                if value in {"function", "class", "const", "let", "export", "import", "return"}:
                    break
                if self.word(cursor) and (self.word(previous) or last in {")", "]", "}"} or type_expression and last == ">"):
                    break
            if value in {"(", "[", "{"} and cursor in self.pairs:
                cursor = self.pairs[cursor]
            previous = cursor
            cursor += 1
        return previous

    def define(self, anchor: int, name: str, kind: str, end: int, first: int | None = None,
               scope: str = "function", *, visible: bool = True, dynamic: set[str] | None = None) -> int:
        prefix = self.owner
        qualified = f"{prefix}.{name}" if prefix else name
        if len(qualified) > 1024 or len(self.scopes) >= 64:
            return end + 1
        if visible:
            self.facts.definitions.append(Definition(
                name.rsplit(".", 1)[-1], qualified, kind, self.tokens[anchor].line,
                self.tokens[end].end_line, "heuristic",
            ))
        if first is None:
            return end + 1
        self.scopes.append(_Scope(qualified, scope, end, kind, dynamic or set()))
        return first

    def reference(self, index: int, target: str, kind: str) -> None:
        if target and len(target) <= 1024:
            self.facts.references.append(Reference(self.owner, target, kind, self.tokens[index].line, "heuristic"))

    def function(self, anchor: int, name: str, params: int, kind: str) -> int | None:
        after = self.pairs[params] + 1
        if self.language in _C_FAMILY and self.val(after) not in _C_TAIL:
            return None
        if self.language in _JS and self.val(after) not in {"{", ":", ";"}:
            return None
        delimiter = self.seek(after, {"{", ";", "=>"})
        if delimiter is None:
            return None
        value = self.val(delimiter)
        if value == "{":
            if delimiter not in self.pairs:
                return None
            end = self.pairs[delimiter]
            # Inline structural return types and member-initializer braces can
            # precede the actual implementation block.
            while self.val(end + 1) in {"{", ","} and self.val(after) not in {"{", ""}:
                further = self.seek(end + 1, {"{", ";"})
                if further is None or self.val(further) != "{" or further not in self.pairs:
                    break
                delimiter, end = further, self.pairs[further]
            return self.define(anchor, name, kind, end, delimiter + 1, dynamic=self.parameter_names(params))
        if value == "=>" and self.language == "csharp" and delimiter + 1 < len(self.tokens):
            start = delimiter + 1
            return self.define(anchor, name, kind, self.expression_end(start), start, dynamic=self.parameter_names(params))
        if value == ";":
            return self.define(anchor, name, "declaration", delimiter)
        return None

    def type_declaration(self, index: int) -> int | None:
        keyword = self.val(index)
        name_at = index + 1
        if keyword == "enum" and self.val(name_at) in {"class", "struct"}:
            name_at += 1
        if keyword == "record" and self.val(name_at) in {"class", "struct"}:
            name_at += 1
        name = self.word(name_at)
        if not name or self.val(index - 1) in _ACCESS:
            return None
        tail = name_at + 1
        kind = keyword
        if keyword == "namespace":
            while self.val(tail) in {".", "::"} and self.word(tail + 1):
                name += "." + self.word(tail + 1)
                tail += 2
        if self.val(tail) == "<":
            generic = self.generic_end(tail)
            if generic is None:
                return None
            tail = generic + 1
        if self.language == "go":
            if self.val(tail) == "[" and tail in self.pairs:
                tail = self.pairs[tail] + 1
            if self.val(tail) in {"struct", "interface"}:
                kind = self.val(tail)
                tail += 1
            elif tail < len(self.tokens):
                end = self.expression_end(tail)
                return self.define(name_at, name, "type", end)
        elif keyword == "type":
            if self.val(tail) != "=" or tail + 1 >= len(self.tokens):
                return None
            tail += 1
            if self.val(tail) != "{":
                return self.define(name_at, name, "type", self.expression_end(tail))
        elif self.language in {"c", "cpp"} and keyword != "namespace":
            # "struct Buffer *read(...)" refers to a type in a return position.
            if self.val(tail) not in {"{", ":", "final"}:
                return None
        delimiter = self.seek(tail, {"{", ";"})
        if delimiter is None:
            return None
        if self.val(delimiter) == "{":
            end = self.pairs.get(delimiter)
            if end is None:
                return None
            scope = "namespace" if keyword in {"namespace", "mod"} else "type"
            return self.define(name_at, name, kind, end, delimiter + 1, scope)
        if keyword == "namespace" and self.language == "csharp":
            return self.define(name_at, name, kind, len(self.tokens) - 1, delimiter + 1, "namespace")
        if (self.language == "rust" and keyword in {"struct", "mod"}) or keyword == "record":
            return self.define(name_at, name, kind, delimiter)
        return None

    def rust_impl(self, index: int) -> int | None:
        start = index + 1
        if self.val(start) == "<":
            generic = self.generic_end(start)
            if generic is None:
                return None
            start = generic + 1
        end = self.seek(start, {"{", ";"})
        if end is None or self.val(end) != "{" or end not in self.pairs:
            return None
        # In "impl<T> Trait<T> for Name<T> where ...", the implemented type
        # begins after "for". Skip argument lists, preserving lexical namespaces.
        cursor = start
        name = ""
        steps = 0
        while cursor < end and steps < _SCAN_LIMIT:
            steps += 1
            value = self.val(cursor)
            if value == "where":
                break
            if value == "for":
                name = ""
            elif value == "<":
                generic = self.generic_end(cursor)
                if generic is None:
                    return None
                cursor = generic
            elif self.word(cursor):
                name = value
            cursor += 1
        if name:
            return self.define(index, name, "impl", self.pairs[end], end + 1, "type", visible=False)
        return None

    def named_function(self, index: int) -> int | None:
        name_at = index + 1
        receiver = ""
        if self.language == "go" and self.val(name_at) == "(":
            close = self.pairs.get(name_at)
            if close is None or not self.word(close + 1):
                return None
            for cursor in range(name_at + 1, min(close, name_at + _SCAN_LIMIT)):
                if self.val(cursor) == "[":
                    break
                if self.word(cursor):
                    receiver = self.word(cursor)
            if not receiver:
                return None
            name_at = close + 1
        if self.val(name_at) == "*":
            name_at += 1
        name = self.word(name_at)
        if not name:
            return None
        params = self.parameters(name_at)
        if params is None:
            return None
        kind = "method" if receiver or self.scope_kind == "type" else "function"
        qualified = f"{receiver}.{name}" if receiver else name
        return self.function(name_at, qualified, params, kind)

    def variable_function(self, name_at: int) -> int | None:
        name = self.word(name_at)
        cursor = name_at + 1
        if not name:
            return None
        if self.val(cursor) == ":":
            equal = self.seek(cursor + 1, {"=", ";"})
            if equal is None:
                return None
            cursor = equal
        if self.val(cursor) != "=":
            return None
        cursor += 1
        if self.scope_kind in {"module", "namespace"} and self.val(cursor) == "{" and cursor in self.pairs:
            return self.define(name_at, name, "object", self.pairs[cursor], cursor + 1, "type")
        if self.val(cursor) == "async":
            cursor += 1
        kind = "method" if self.scope_kind == "type" else "function"
        if self.val(cursor) == "function":
            cursor += 1
            if self.val(cursor) == "*":
                cursor += 1
            if self.word(cursor):
                cursor += 1
            if self.val(cursor) == "(" and cursor in self.pairs:
                return self.function(name_at, name, cursor, kind)
            return None
        if self.val(cursor) == "<":
            generic = self.generic_end(cursor)
            if generic is None:
                return None
            cursor = generic + 1
        if self.val(cursor) == "(" and cursor in self.pairs:
            param_start = cursor
            cursor = self.pairs[cursor] + 1
        elif self.word(cursor):
            param_start = cursor
            cursor += 1
        else:
            return None
        if self.val(cursor) == ":":
            arrow = self.seek(cursor + 1, {"=>", ";"})
            if arrow is None:
                return None
            cursor = arrow
        if self.val(cursor) != "=>" or cursor + 1 >= len(self.tokens):
            return None
        dynamic = self.parameter_names(param_start) if self.val(param_start) == "(" else {self.word(param_start)}
        first = cursor + 1
        if self.val(first) == "{":
            if first in self.pairs:
                return self.define(name_at, name, kind, self.pairs[first], first + 1, dynamic=dynamic)
            return None
        return self.define(name_at, name, kind, self.expression_end(first), first, dynamic=dynamic)

    def member_or_free_function(self, index: int, params: int) -> int | None:
        if self.scope_kind == "function":
            return None
        if (self.language in {"go", "typescript"} and self.scopes and self.scopes[-1].syntax == "interface"
                and self.val(index - 1) not in {".", ":", "="}):
            end = self.pairs[params]
            following = end + 1
            if self.language == "typescript" and self.val(following) == ":" and following + 1 < len(self.tokens):
                end = self.expression_end(following + 1, type_expression=True)
            elif (self.language == "go" and following < len(self.tokens) and self.val(following) not in {";", "}"}
                    and self.tokens[following].line == self.tokens[end].line):
                end = self.expression_end(following)
            return self.define(index, self.word(index), "declaration", end)
        if self.language in _JS:
            if self.scope_kind != "type" or self.val(index - 1) in {"=", ".", "?.", "@", ":"}:
                return None
            return self.function(index, self.word(index), params, "method")
        if self.language not in _C_FAMILY:
            return None
        name = self.word(index)
        start = index
        if self.val(start - 1) == "~":
            start -= 1
            name = "~" + name
        while self.val(start - 1) == "::" and self.word(start - 2):
            name = self.word(start - 2) + "." + name
            start -= 2
        before = self.val(start - 1)
        constructor = self.scope_kind == "type" and name.lstrip("~") == self.owner.rsplit(".", 1)[-1]
        if not constructor and "." in name:
            parts = name.split(".")
            constructor = parts[-1].lstrip("~") == parts[-2]
        # A return-type token, pointer/reference qualifier, or constructor
        # distinguishes a declaration from a call in a field initializer.
        typed = bool(self.word(start - 1)) and before not in _NON_CALL - {"const", "static"}
        if not constructor and not typed and before not in {"*", "&", "&&", ">", "]", "?"}:
            return None
        kind = "method" if self.scope_kind == "type" or "." in name else "function"
        return self.function(index, name, params, kind)

    def call(self, index: int) -> None:
        target = self.word(index)
        cursor = index
        while self.val(cursor - 1) in _ACCESS:
            receiver = self.word(cursor - 2)
            if not receiver or len(target) > 1024:
                return  # A computed receiver can't be represented as a dotted name.
            target = receiver + self.val(cursor - 1) + target
            cursor -= 2
        root = self.word(cursor)
        bound = any(root in scope.dynamic for scope in reversed(self.scopes))
        known_receiver = root in {"self", "this"} and cursor + 2 == index
        nested_receiver = root in {"self", "this"} and cursor + 2 < index
        kind = "dynamic_call" if (bound and not known_receiver) or nested_receiver else "call"
        self.reference(index, target, kind)

    def imports(self, index: int) -> int | None:
        language = self.language
        word = self.val(index)
        if language in _JS and word in {"import", "export", "require"}:
            if self.val(index - 1) in _ACCESS:
                return None
            if self.val(index + 1) == "(":
                if word == "export" or self.pairs.get(index + 1) != index + 3:
                    return None
                if self.tokens[index + 2].kind == "string":
                    self.reference(index + 2, self.tokens[index + 2].value, "import")
                    return index + 4
                return None
            if word == "require" or word == "export" and self.val(index + 1) not in {"{", "*"}:
                return None
            cursor = index + 1
            if cursor < len(self.tokens) and self.tokens[cursor].kind == "string":
                self.reference(cursor, self.tokens[cursor].value, "import")
                return cursor + 1
            marker = self.seek(cursor, {"from", ";", "="})
            if marker is not None and self.val(marker) == "from" and marker + 1 < len(self.tokens):
                module = self.tokens[marker + 1]
                if module.kind == "string":
                    self.reference(marker + 1, module.value, "import")
                    return marker + 2
        elif language == "go" and word == "import":
            start = index + 1
            if self.val(start) == "(" and start in self.pairs:
                end = self.pairs[start]
            else:
                end = start + int(bool(self.word(start)) or self.val(start) == ".")
            if end >= len(self.tokens):
                return None
            for cursor in range(start, end + 1):
                module = self.tokens[cursor]
                if module.kind == "string":
                    self.reference(cursor, module.value, "import")
            return end + 1
        elif language == "rust" and word == "use":
            end = self.seek(index + 1, {";"})
            if end is not None and end - index <= _SCAN_LIMIT:
                target = "".join(" as " if self.val(i) == "as" else self.val(i) for i in range(index + 1, end))
                self.reference(index, target, "import")
                return end + 1
        elif (language == "java" and word == "import") or (language == "csharp" and word == "using"):
            cursor = index + 1
            if self.val(cursor) == "static":
                cursor += 1
            if self.word(cursor) and self.val(cursor + 1) == "=":
                cursor += 2
            start = cursor
            if not self.word(start):
                return None
            cursor += 1
            while self.val(cursor) in {".", "::"} and (self.word(cursor + 1) or self.val(cursor + 1) == "*"):
                cursor += 2
            if self.val(cursor) == ";":
                self.reference(index, "".join(self.val(i) for i in range(start, cursor)), "import")
                return cursor + 1
        return None

    def declaration(self, index: int) -> int | None:
        value = self.val(index)
        if value in _TYPE_KEYWORDS[self.language]:
            return self.type_declaration(index)
        if self.language == "rust":
            if value == "impl":
                return self.rust_impl(index)
            if value == "fn":
                return self.named_function(index)
        if self.language == "go" and value == "func":
            return self.named_function(index)
        if self.language in _JS:
            if value == "function":
                return self.named_function(index)
            if value in {"var", "let", "const"}:
                return self.variable_function(index + 1)
            if self.scope_kind == "type" and self.val(index + 1) == "=":
                return self.variable_function(index)
        if self.language in {"c", "cpp"} and value == "typedef":
            return self.typedef(index)
        return None

    def typedef(self, index: int) -> int | None:
        kind = self.val(index + 1)
        tag_at = index + 2
        body = tag_at + int(bool(self.word(tag_at)))
        if kind in {"struct", "enum", "union"} and self.val(body) == "{" and body in self.pairs:
            close = self.pairs[body]
            alias = self.word(close + 1)
            tag = self.word(tag_at)
            if tag and alias and tag != alias:
                self.define(close + 1, alias, "type", close + 1)
            if tag or alias:
                return self.define(tag_at if tag else index, tag or alias, kind, close + bool(alias), body + 1, "type")
        end = self.seek(index + 1, {";", "{"})
        if end is None or self.val(end) != ";" or end - index > _SCAN_LIMIT:
            return None
        for cursor in range(index + 1, end):
            if self.val(cursor) == "(" and self.val(cursor + 1) == "*" and self.word(cursor + 2):
                return self.define(cursor + 2, self.word(cursor + 2), "type", end)
            if self.word(cursor) and self.val(cursor + 1) in {"(", "["} and cursor + 1 in self.pairs:
                if self.pairs[cursor + 1] == end - 1:
                    return self.define(cursor, self.word(cursor), "type", end)
        name = self.word(end - 1)
        return self.define(end - 1, name, "type", end) if name else None

    def run(self) -> None:
        index = 0
        while index < len(self.tokens):
            while self.scopes and self.scopes[-1].end < index:
                self.scopes.pop()
            token = self.tokens[index]
            if self.language == "rust" and self.val(index) == "#":
                attribute = index + 1 + int(self.val(index + 1) == "!")
                if self.val(attribute) == "[" and attribute in self.pairs:
                    index = self.pairs[attribute] + 1
                    continue
            if self.language == "java" and self.val(index) == "@" and self.word(index + 1) not in {"", "interface"}:
                cursor = index + 2
                while self.val(cursor) == "." and self.word(cursor + 1):
                    cursor += 2
                index = self.pairs[cursor] + 1 if self.val(cursor) == "(" and cursor in self.pairs else cursor
                continue
            if token.kind == "directive":
                if match := _INCLUDE.match(token.value):
                    self.reference(index, match[1], "import")
                elif match := _MACRO.match(token.value):
                    self.define(index, match[1], "macro", index)
            if token.kind == "word":
                next_index = self.imports(index) if token.value in {"import", "export", "require", "use", "using"} else None
                if next_index is None:
                    next_index = self.declaration(index)
                if next_index is None and self.scope_kind == "function":
                    if self.language in _PREFIX_PARAMETERS and token.value in {"var", "const", "let"}:
                        name = self.word(index + 1 + int(self.val(index + 1) == "mut"))
                        if name:
                            self.scopes[-1].dynamic.add(name)
                if (next_index is None and token.value not in _NON_CALL
                        and (self.language not in _TYPED_LANGUAGES or token.value not in _PRIMITIVES)):
                    params = self.parameters(index)
                    if params is not None:
                        next_index = self.member_or_free_function(index, params)
                        if next_index is None:
                            self.call(index)
                if next_index is not None:
                    index = next_index
                    continue
            index += 1


def extract_braced(text: str, facts: Facts, ambiguous_header: bool = False) -> None:
    tokens = tokenize(text, facts.language)
    if ambiguous_header and any(
        a.kind == "word" and ((a.value in {"class", "namespace"} and b.kind == "word")
                               or (a.value == "template" and b.value == "<"))
        for a, b in zip(tokens, tokens[1:])
    ):
        facts.language = "cpp"
    _Adapter(tokens, facts).run()
