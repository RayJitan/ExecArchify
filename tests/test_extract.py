import unittest

from execarchify.extract import MAX_SOURCE_CHARS, extract


def definitions(facts):
    return [(d.qualified, d.kind, d.line, d.end_line) for d in facts.definitions]


def references(facts):
    return [(r.source, r.target, r.kind, r.line) for r in facts.references]


class PythonFactsTests(unittest.TestCase):
    def test_nested_definitions_owners_and_imports(self):
        source = (
            "from .handlers import (\n"
            "    ready as is_ready,\n"
            "    emit,\n"
            ")\n"
            "@installed(factory())\n"
            "class Box:\n"
            "    async def check(\n"
            "        self, value=default(),\n"
            "    ):\n"
            "        import pkg.log\n"
            "        def inner():\n"
            "            return emit()\n"
            "        self.finish(inner())\n"
        )
        facts = extract("src/box.py", source)
        self.assertEqual(facts.language, "python")
        self.assertEqual(definitions(facts), [
            ("Box", "class", 6, 13), ("Box.check", "method", 7, 13), ("Box.check.inner", "function", 11, 12),
        ])
        self.assertEqual(references(facts), [
            ("", ".handlers.ready", "import", 2), ("", ".handlers.emit", "import", 3),
            ("", "installed", "call", 5), ("", "factory", "call", 5), ("Box", "default", "call", 8),
            ("Box.check", "pkg.log", "import", 10), ("Box.check.inner", "emit", "call", 12),
            ("Box.check", "self.finish", "call", 13), ("Box.check", "inner", "call", 13),
        ])
        self.assertTrue(all(item.confidence == "parsed" for item in [*facts.definitions, *facts.references]))

    def test_shadowed_and_computed_callees_have_no_unique_target(self):
        source = (
            "def dispatch(cb, peer, key):\n"
            "    def inner():\n"
            "        cb()\n"
            "    peer.run()\n"
            "    '起点'; table[key]()\n"
            "    factory().send()\n"
            "    local = select()\n"
            "    local()\n"
            "    return inner()\n"
        )
        refs = references(extract("events.py", source))
        self.assertIn(("dispatch.inner", "cb", "dynamic_call", 3), refs)
        self.assertIn(("dispatch", "peer.run", "dynamic_call", 4), refs)
        self.assertIn(("dispatch", "table[key]", "dynamic_call", 5), refs)
        self.assertIn(("dispatch", "factory().send", "dynamic_call", 6), refs)
        self.assertIn(("dispatch", "factory", "call", 6), refs)
        self.assertIn(("dispatch", "local", "dynamic_call", 8), refs)
        self.assertIn(("dispatch", "inner", "call", 9), refs)

    def test_invalid_tail_preserves_complete_prefix(self):
        source = 'def good():\n    return work()\n\n@decorated\ndef broken(:\n'
        facts = extract("partial.py", source)
        self.assertEqual(definitions(facts), [("good", "function", 1, 2)])
        self.assertEqual(references(facts), [("good", "work", "call", 2)])
        self.assertEqual(extract("partial.py", '"""def fake():').definitions, [])


class BraceFactsTests(unittest.TestCase):
    def test_callbacks_and_nested_receivers_keep_dispatch_unresolved(self):
        js = extract("button.js", "class Button { run(cb) { cb(); this.view.flush(); this.tick(); } }")
        self.assertEqual(references(js), [
            ("Button.run", "cb", "dynamic_call", 1),
            ("Button.run", "this.view.flush", "dynamic_call", 1),
            ("Button.run", "this.tick", "call", 1),
        ])
        c = extract("io.c", "void run(void (*done)(int)) { done(2); }")
        self.assertEqual(references(c), [("run", "done", "dynamic_call", 1)])
        java = extract("Job.java", 'class Job { @Frame("start") void run(Executor e) { e.start(); } }')
        self.assertEqual(references(java), [("Job.run", "e.start", "dynamic_call", 1)])

    def test_header_dialect_and_go_interface_declarations(self):
        header = extract("io.h", "/* class Fake {} */\nclass Store { public: int read(); };")
        self.assertEqual(header.language, "cpp")
        self.assertEqual(definitions(header), [("Store", "class", 2, 2), ("Store.read", "declaration", 2, 2)])
        go = extract("io.go", "type Service interface {\n Read(ctx Context) int\n Close()\n}\n")
        self.assertEqual(definitions(go), [
            ("Service", "interface", 1, 4), ("Service.Read", "declaration", 2, 2),
            ("Service.Close", "declaration", 3, 3),
        ])
        self.assertEqual(go.references, [])

    def test_typescript_interface_and_named_object_methods(self):
        facts = extract("api.ts", (
            "interface Api {\n  run?(): Promise<void>\n  value(): {id: string}\n}\n"
            "export const api = {\n  run() { execute(); },\n};\n"
        ))
        self.assertEqual(definitions(facts), [
            ("Api", "interface", 1, 4), ("Api.run", "declaration", 2, 2), ("Api.value", "declaration", 3, 3),
            ("api", "object", 5, 7), ("api.run", "method", 6, 6),
        ])
        self.assertEqual(references(facts), [("api.run", "execute", "call", 6)])

    def test_c_type_and_callback_aliases(self):
        facts = extract("io.h", (
            "typedef unsigned long Count;\n"
            "typedef void (*OnReady)(int value);\n"
            "typedef struct RawItem { int id; } Item;\n"
        ))
        self.assertEqual(set(definitions(facts)), {
            ("Count", "type", 1, 1), ("OnReady", "type", 2, 2),
            ("RawItem", "struct", 3, 3), ("Item", "type", 3, 3),
        })
        self.assertEqual(facts.references, [])

    def test_typescript_syntax_strings_comments_and_expression_arrow(self):
        source = (
            'import { load } from "./io";\n'
            'export class Store<T> {\n'
            '  async handle(\n'
            '    x: T,\n'
            '  ): Promise<void> {\n'
            '    const literal = `fake() { }`;\n'
            '    /* else { spoof(); } */ this.emit(load(x));\n'
            '    return /fake\\(/.test(x);\n'
            '  }\n'
            '}\n'
            'export const ping = (x: number) =>\n'
            '  dispatch(x);\n'
            'flush();\n'
        )
        facts = extract("ui/store.tsx", source)
        self.assertEqual(definitions(facts), [
            ("Store", "class", 2, 10), ("Store.handle", "method", 3, 9), ("ping", "function", 11, 12),
        ])
        self.assertEqual(references(facts), [
            ("", "./io", "import", 1), ("Store.handle", "this.emit", "call", 7),
            ("Store.handle", "load", "call", 7), ("ping", "dispatch", "call", 12),
            ("", "flush", "call", 13),
        ])
        self.assertTrue(all(item.confidence == "heuristic" for item in [*facts.definitions, *facts.references]))

    def test_javascript_assignments_and_commonjs(self):
        source = (
            'const io = require("./io");\n'
            'export function entry(x) {\n'
            '  const inside = value => send(value);\n'
            '  return inside(x);\n'
            '}\n'
        )
        facts = extract("run.mjs", source)
        self.assertEqual(facts.language, "javascript")
        self.assertEqual(definitions(facts), [("entry", "function", 2, 5), ("entry.inside", "function", 3, 3)])
        self.assertEqual(references(facts), [
            ("", "./io", "import", 1), ("entry.inside", "send", "call", 3), ("entry", "inside", "call", 4),
        ])

    def test_go_receivers_generics_and_imports(self):
        facts = extract("store.go", (
            'package store\n'
            'import (\n  "fmt"\n  w "example/wire"\n)\n'
            'type Sink[T any] struct { item T }\n'
            'func (s *Sink[T]) Run(\n  value T,\n) error {\n'
            '  return w.Send(fmt.Sprint(value))\n}\n'
        ))
        self.assertEqual(definitions(facts), [("Sink", "struct", 6, 6), ("Sink.Run", "method", 7, 11)])
        self.assertEqual(references(facts), [
            ("", "fmt", "import", 3), ("", "example/wire", "import", 4),
            ("Sink.Run", "w.Send", "call", 10), ("Sink.Run", "fmt.Sprint", "call", 10),
        ])

    def test_rust_trait_impl_raw_string_nested_comment_and_lifetime(self):
        facts = extract("queue.rs", (
            'use crate::wire::emit;\n'
            "struct Queue<'a> { name: &'a str }\n"
            "impl<'a> Runner for Queue<'a> {\n"
            '  fn run(&self) {\n'
            '    let msg = r##" fake() {} "##;\n'
            '    /* outer /* nested */ ignored(); */ crate::start();\n'
            '    emit();\n  }\n}\n'
        ))
        self.assertEqual(definitions(facts), [("Queue", "struct", 2, 2), ("Queue.run", "method", 4, 8)])
        self.assertEqual(references(facts), [
            ("", "crate::wire::emit", "import", 1), ("Queue.run", "crate::start", "call", 6),
            ("Queue.run", "emit", "call", 7),
        ])

    def test_c_header_prototype_macro_and_source(self):
        header = extract("api.h", (
            '#include <stddef.h>\n#define WRAP(x) \\\n  unused_in_macro(x)\n'
            'typedef struct {\n  int size;\n} Packet;\nvoid accept(Packet *p);\n'
        ))
        self.assertEqual(definitions(header), [("WRAP", "macro", 2, 3), ("Packet", "struct", 4, 6),
                                                ("accept", "declaration", 7, 7)])
        self.assertEqual(references(header), [("", "stddef.h", "import", 1)])
        code = extract("api.c", (
            '#include "api.h"\nstatic int receive(\n  Packet *p\n) {\n'
            '  const char *s = "misleading() }";\n  hooks->notify();\n  return next(p);\n}\n'
        ))
        self.assertEqual(definitions(code), [("receive", "function", 2, 8)])
        self.assertEqual(references(code), [
            ("", "api.h", "import", 1), ("receive", "hooks->notify", "call", 6),
            ("receive", "next", "call", 7),
        ])

    def test_cpp_namespaces_and_out_of_class_definition(self):
        facts = extract("engine.cpp", (
            "namespace render {\n"
            "class Engine { public: void tick(); };\n"
            "void Engine::tick() {\n"
            '  auto s = R"txt(noise(); })txt";\n'
            "  backend::send();\n}\n}\n"
        ))
        self.assertEqual(definitions(facts), [
            ("render", "namespace", 1, 7), ("render.Engine", "class", 2, 2),
            ("render.Engine.tick", "declaration", 2, 2), ("render.Engine.tick", "method", 3, 6),
        ])
        self.assertEqual(references(facts), [("render.Engine.tick", "backend::send", "call", 5)])

    def test_java_and_csharp_methods(self):
        java = extract("Task.java", "import java.util.List;\nclass Task {\n void run() throws Exception {\n  send();\n }\n}")
        self.assertEqual(definitions(java), [("Task", "class", 2, 6), ("Task.run", "method", 3, 5)])
        self.assertEqual(references(java), [("", "java.util.List", "import", 1), ("Task.run", "send", "call", 4)])
        cs = extract("Task.cs", "using Lib.IO;\nnamespace App.Core;\nclass Task {\n int Run() => Read();\n}")
        self.assertEqual(definitions(cs), [
            ("App.Core", "namespace", 2, 5), ("App.Core.Task", "class", 3, 5),
            ("App.Core.Task.Run", "method", 4, 4),
        ])
        self.assertEqual(references(cs), [("", "Lib.IO", "import", 1), ("App.Core.Task.Run", "Read", "call", 4)])


class DocumentFactsTests(unittest.TestCase):
    def test_markdown_ranges_and_fences(self):
        facts = extract("guide.md", "# Setup\nintro\n## Run\n```sh\n# Fake\n```\n\nOther\n=====\n")
        self.assertEqual(definitions(facts), [
            ("Setup", "heading", 1, 7), ("Setup.Run", "heading", 3, 7), ("Other", "heading", 8, 9),
        ])
        rst = extract("guide.rst", "Overview\n========\nA paragraph\n")
        self.assertEqual(definitions(rst), [("Overview", "heading", 1, 3)])

    def test_config_keys_skip_literal_content(self):
        yaml = extract("settings.yml", "app:\n  pool:\n    size: 4\n  notes: |\n    fake: here\nother: ok\n")
        self.assertEqual([d.qualified for d in yaml.definitions], ["app", "app.pool", "app.pool.size", "app.notes", "other"])
        toml = extract("settings.toml", '[tool.demo]\nname="demo"\nnotes="""\nfake=1\n"""\n')
        self.assertEqual([d.qualified for d in toml.definitions], ["tool.demo", "tool.demo.name", "tool.demo.notes"])
        packed = extract("package.json", '{"modules":[{"name":"\\"fake\\": 2"}],\n "runs": 4}')
        self.assertEqual(definitions(packed), [
            ("modules", "key", 1, 1), ("modules.name", "key", 1, 1), ("runs", "key", 2, 2),
        ])

    def test_unknown_files_shebang_and_bounds(self):
        self.assertEqual(extract("notes.odd", "task(example)\n").language, "text")
        self.assertEqual(extract("notes.odd", "task(example)\n").definitions, [])
        facts = extract("bin/task", "#!/usr/bin/env python3\r\ndef run():\r\n    pass\r\n")
        self.assertEqual(facts.language, "python")
        self.assertEqual(definitions(facts), [("run", "function", 2, 3)])
        self.assertEqual(extract("dense.py", "a=" + "+" * 5_000 + "1").definitions, [])
        oversized_name = "def " + "a" * 100_000 + "():\n    f()\n" + "    f()\n" * 100
        self.assertEqual(extract("dense.py", oversized_name).references, [])
        self.assertEqual(extract("big.go", "x" * (MAX_SOURCE_CHARS + 1)).references, [])
        self.assertEqual(extract("oops.ts", "function f( { /* unterminated\n").language, "typescript")

    def test_build_manifest_anchors_and_malformed_data(self):
        make = extract("Makefile", ".PHONY: build\nbuild: base\n\ttool build: arg\n")
        self.assertEqual(definitions(make), [(".PHONY", "target", 1, 1), ("build", "target", 2, 2)])
        image = extract("Dockerfile.test", "FROM base AS compile\nRUN echo x\nFROM compile as product\n")
        self.assertEqual(definitions(image), [("compile", "stage", 1, 1), ("product", "stage", 3, 3)])
        truncated = extract("large.json", '"' + '\\"' * 8_000)
        self.assertEqual(truncated.definitions, [])


if __name__ == "__main__":
    unittest.main()
