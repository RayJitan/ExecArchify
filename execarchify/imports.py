"""Match explicit import spellings to local files without executing project loaders."""

from pathlib import PurePosixPath
import posixpath


def import_paths(language, source, target):
    parent = posixpath.dirname(source)
    if language in ("javascript", "typescript") and target.startswith(("./", "../")):
        base = posixpath.normpath(posixpath.join(parent, target))
        if base.startswith("../"):
            return []
        extensions = (
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
            ".mts",
            ".cts",
            ".json",
        )
        if PurePosixPath(base).suffix:
            return [base]
        return [base + ext for ext in extensions] + [
            base + "/index" + ext for ext in extensions
        ]
    if language in ("c", "cpp") and target and not posixpath.isabs(target):
        return list(
            dict.fromkeys((posixpath.normpath(posixpath.join(parent, target)), target))
        )
    if language == "python":
        prefix = len(target) - len(target.lstrip("."))
        module = target[prefix:].replace(".", "/")
        if prefix:
            parent_dirs = PurePosixPath(parent).parts
            if prefix > len(parent_dirs) + 1:
                return []
            base = posixpath.join(
                *([*parent_dirs[: len(parent_dirs) - prefix + 1], module] or ["."])
            )
            return [base + ".py", base + "/__init__.py"]
        return [
            p
            for root in ("", "src/")
            for p in (root + module + ".py", root + module + "/__init__.py")
        ]
    return []
