"""Extract source positions and spellings from one file, without executing its code."""

import ast
from dataclasses import dataclass
import io
from pathlib import PurePath
import re
import tokenize

from .languages.braces import extract_braced
from .languages.documents import extract_document
from .model import Definition, Facts, Reference


# Larger or unusually dense inputs remain available to the file/text index.
MAX_SOURCE_CHARS = 2_000_000
_PYTHON_TOKEN_LIMIT = 120_000
_PYTHON_STATEMENT_LIMIT = 1_500

_EXTENSIONS = {
    "python": "py pyi pyw",
    "javascript": "js jsx mjs cjs",
    "typescript": "ts tsx mts cts",
    "go": "go",
    "rust": "rs",
    "c": "c h",
    "cpp": "cc cp cpp cxx c++ hh hpp hxx h++ inl ipp tpp",
    "java": "java",
    "csharp": "cs",
    "markdown": "md markdown mdx",
    "rst": "rst",
    "yaml": "yaml yml",
    "toml": "toml",
    "ini": "ini cfg",
    "properties": "properties",
    "json": "json",
    "make": "mk",
    "html": "html htm",
    "xml": "xml xsd svg",
    "css": "css",
    "sql": "sql",
    "shell": "sh bash zsh",
    "powershell": "ps1 psm1 psd1",
    "ruby": "rb",
    "swift": "swift",
    "kotlin": "kt kts",
    "lua": "lua",
    "verilog": "v vh",
    "systemverilog": "sv svh",
    "vhdl": "vhd vhdl",
}
_BY_EXTENSION = {ext: lang for lang, extensions in _EXTENSIONS.items() for ext in extensions.split()}
_BRACED = {"javascript", "typescript", "go", "rust", "c", "cpp", "java", "csharp"}


def _language(path: str, text: str) -> str:
    name = PurePath(path.replace("\\", "/")).name
    folded = name.lower()
    if folded in {"makefile", "gnumakefile", "bsdmakefile"}:
        return "make"
    if folded == "dockerfile" or folded.startswith("dockerfile."):
        return "dockerfile"
    if name.endswith((".C", ".H")):
        return "cpp"
    language = _BY_EXTENSION.get(PurePath(folded).suffix[1:])
    if language:
        return language
    if text.startswith("#!"):
        first_line = text[:256].split("\n", 1)[0]
        match = re.search(r"(?:[/\s])(python(?:[23](?:\.\d+)*)?|node(?:js)?|ba?sh|sh|zsh)(?=\s|$)", first_line)
        if match:
            command = match[1]
            if command.startswith("python"):
                return "python"
            return "javascript" if command.startswith("node") else "shell"
    return "text"


def _python_budget(text: str) -> bool:
    # Short snippets cannot exceed either token limit. CPython's AST builder is
    # recursive even when its consumer is iterative (e.g. a long unary chain).
    if len(text) <= _PYTHON_STATEMENT_LIMIT:
        return True
    in_statement = 0
    try:
        for total, item in enumerate(tokenize.generate_tokens(io.StringIO(text).readline), start=1):
            in_statement += 1
            if item.type == tokenize.NEWLINE:
                in_statement = 0
            if total > _PYTHON_TOKEN_LIMIT or in_statement > _PYTHON_STATEMENT_LIMIT:
                return False
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass  # The parser can still recover the complete preceding declarations.
    return True


def _python_tree(text: str) -> ast.Module | None:
    if not _python_budget(text):
        return None
    try:
        return ast.parse(text)
    except SyntaxError as error:
        # A partial checkout or an edit can break the tail of a module. Only
        # trust a prefix that parses independently, ending at a top-level line.
        lines = text.splitlines(keepends=True)
        last = min((error.lineno or 1) - 1, len(lines) - 1)
        attempts = 0
        for index in range(last, 0, -1):
            line = lines[index]
            if not line.strip() or line[0].isspace() or line.startswith("#"):
                continue
            try:
                return ast.parse("".join(lines[:index]))
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                attempts += 1
                if attempts >= 3:
                    break
    except (ValueError, RecursionError, MemoryError):
        pass
    return None


def _python_target(node: ast.expr) -> str:
    parts = []
    while isinstance(node, ast.Attribute) and len(parts) < 64:
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return ""


@dataclass(slots=True)
class _Bindings:
    owner: str
    kind: str
    parent: "_Bindings | None"
    dynamic: set[str]
    named: set[str]
    receiver: str

    def dynamic_target(self, spelling: str) -> bool:
        root, _, member = spelling.partition(".")
        scope = self
        while scope is not None:
            if root in scope.dynamic:
                return not (member and "." not in member and root == scope.receiver)
            if root in scope.named:
                break
            scope = scope.parent
        return False


def _bindings(node, owner, kind, parent) -> _Bindings:
    dynamic = set()
    named = set()
    receiver = ""
    args = getattr(node, "args", None)
    if args:
        positional = args.posonlyargs + args.args
        for param in [*positional, *args.kwonlyargs, args.vararg, args.kwarg]:
            if param:
                dynamic.add(param.arg)
        if kind == "method" and positional and positional[0].arg in {"self", "cls"}:
            static = any(_python_target(decorator) in {"staticmethod", "builtins.staticmethod"}
                         for decorator in getattr(node, "decorator_list", []))
            if not static:
                receiver = positional[0].arg
    pending = list(node.body) if isinstance(node.body, list) else [node.body]
    while pending:
        child = pending.pop()
        if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            named.add(child.name)
            continue
        if isinstance(child, (ast.Lambda, ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)):
            continue
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            dynamic.add(child.id)
            if child.id == receiver:
                receiver = ""
        elif isinstance(child, ast.ExceptHandler) and child.name:
            dynamic.add(child.name)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            named.update(item.asname or item.name.split(".", 1)[0] for item in child.names)
        pending.extend(ast.iter_child_nodes(child))
    # A method's free names are resolved in enclosing function/module scopes.
    while parent is not None and parent.kind == "class":
        parent = parent.parent
    return _Bindings(owner, kind, parent, dynamic, named, receiver)


def _computed_spelling(lines: list[bytes], expression: ast.expr) -> str:
    first = expression.lineno - 1
    last = (expression.end_lineno or expression.lineno) - 1
    if last - first > 8 or expression.end_col_offset is None:
        return ""
    # AST byte columns index UTF-8, including when a preceding literal/name
    # on this line contains a multibyte character.
    if first == last:
        if expression.end_col_offset - expression.col_offset > 512:
            return ""
        raw = lines[first][expression.col_offset:expression.end_col_offset]
    else:
        parts = [lines[first][expression.col_offset:], *lines[first + 1:last], lines[last][:expression.end_col_offset]]
        if sum(map(len, parts)) > 512:
            return ""
        raw = b"\n".join(parts)
    return raw.decode("utf-8")


def _python(text: str, facts: Facts) -> None:
    tree = _python_tree(text)
    if tree is None:
        return
    # Defaults, annotations, bases and decorators appear in the enclosing
    # scope. Only body statements acquire the new function/class as owner.
    lines = text.encode("utf-8").split(b"\n")
    pending = [(tree, _bindings(tree, "", "module", None))]
    while pending:
        node, scope = pending.pop()
        owner = scope.owner
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if isinstance(node, ast.ClassDef):
                kind = "class"
            else:
                kind = "method" if scope.kind == "class" else "function"
            qualified = f"{owner}.{node.name}" if owner else node.name
            if len(qualified) > 1024:
                continue
            body_scope = _bindings(node, qualified, kind, scope)
            facts.definitions.append(Definition(node.name, qualified, kind, node.lineno, node.end_lineno or node.lineno))
            for field, children in ast.iter_fields(node):
                if field == "body":
                    pending.extend((child, body_scope) for child in reversed(children))
                elif isinstance(children, list):
                    pending.extend((child, scope) for child in reversed(children) if isinstance(child, ast.AST))
                elif isinstance(children, ast.AST):
                    pending.append((children, scope))
            continue
        if isinstance(node, ast.Lambda):
            pending.append((node.args, scope))
            pending.append((node.body, _bindings(node, owner, "lambda", scope)))
            continue
        if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)):
            dynamic = {name.id for generator in node.generators for name in ast.walk(generator.target)
                       if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)}
            local = _Bindings(owner, "comprehension", scope, dynamic, set(), "")
            for index, generator in enumerate(node.generators):
                pending.append((generator.iter, scope if index == 0 else local))
                pending.extend((condition, local) for condition in generator.ifs)
            if isinstance(node, ast.DictComp):
                pending.extend([(node.key, local), (node.value, local)])
            else:
                pending.append((node.elt, local))
            continue
        if isinstance(node, ast.Call):
            target = _python_target(node.func)
            kind = "call"
            if not target:
                target = _computed_spelling(lines, node.func)
                kind = "dynamic_call"
            elif scope.dynamic_target(target):
                kind = "dynamic_call"
            if target and len(target) <= 1024:
                facts.references.append(Reference(owner, target, kind, node.func.lineno))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            prefix = ""
            if isinstance(node, ast.ImportFrom):
                prefix = "." * node.level + (node.module or "")
                if prefix and not prefix.endswith("."):
                    prefix += "."
            for item in node.names:
                target = prefix + item.name
                if len(target) <= 1024:
                    facts.references.append(Reference(owner, target, "import", item.lineno))
        pending.extend((child, scope) for child in reversed(list(ast.iter_child_nodes(node))))


def extract(path: str, text: str) -> Facts:
    """Return file-local facts. File bodies remain searchable even with no facts."""
    language = _language(path, text)
    facts = Facts(language)
    if len(text) > MAX_SOURCE_CHARS or "\x00" in text:
        return facts
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.removeprefix("\ufeff")
    if language == "python":
        _python(text, facts)
    elif language in _BRACED:
        extract_braced(text, facts, language == "c" and path.endswith(".h"))
    else:
        extract_document(text, facts)
    facts.definitions.sort(key=lambda item: item.line)
    facts.references.sort(key=lambda item: item.line)
    return facts
