import codecs
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from execarchify import index
from execarchify.model import connect


def git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *map(str, args)], capture_output=True, check=False
    )
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))
    return result.stdout.decode("utf-8").strip()


def new_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "Index Test"),
        ("user.email", "index@example.invalid"),
        ("commit.gpgsign", "false"),
        ("core.autocrlf", "false"),
    ):
        git(path, "config", key, value)
    return path


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def commit(root, message):
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = new_repo(self.home / "business")
        self.ws = index.discover(self.root)

    def rows(self, sql, *params):
        with closing(connect(self.ws)) as db:
            return [tuple(r) for r in db.execute(sql, params)]

    def value(self, key):
        return self.rows("SELECT value FROM meta WHERE key=?", key)[0][0]

    def test_edit_delete_rename_untrack_and_search_index(self):
        write(self.root, "a.py", "def caller():\n    old()\n")
        write(self.root, "sub/b.py", "def old():\n    pass\n")
        head = commit(self.root, "base")
        self.assertEqual(index.discover(self.root / "sub" / "b.py"), self.ws)
        self.assertEqual(index.synchronize(self.ws)["added"], 2)
        self.assertEqual(
            self.rows("SELECT revision,binding FROM repositories"), [(head, "worktree")]
        )
        original = self.rows("SELECT id FROM symbols WHERE name='caller'")[0][0]
        git(self.root, "mv", "sub/b.py", "renamed.py")
        write(self.root, "a.py", "\n\ndef caller():\n    fresh()\n")
        write(self.root, "new file.py", "def fresh():\n    return 1\n")
        update = index.synchronize(self.ws)
        self.assertEqual(
            (update["added"], update["updated"], update["deleted"]), (2, 1, 1)
        )
        self.assertEqual(
            self.rows("SELECT id FROM symbols WHERE name='caller'"), [(original,)]
        )
        self.assertEqual(
            self.rows("SELECT target FROM refs WHERE source='caller'"), [("fresh",)]
        )
        delta = json.loads(self.value("last_delta"))
        dead = next(r for r in delta if r["path"] == "sub/b.py")
        self.assertEqual(
            (dead["status"], dead["old_symbols"][0]["name"]), ("deleted", "old")
        )
        edited = next(r for r in delta if r["path"] == "a.py")
        self.assertEqual(edited["old_refs"][0]["target"], "old")
        self.assertEqual(
            self.rows(
                "SELECT path FROM files_fts WHERE files_fts MATCH 'fresh' ORDER BY path"
            ),
            [("a.py",), ("new file.py",)],
        )
        with (
            patch.object(index, "extract", side_effect=AssertionError("warm parse")),
            patch.object(
                index, "_read_worktree_file", side_effect=AssertionError("warm read")
            ),
        ):
            warm = index.synchronize(self.ws)
        self.assertEqual(warm["generation"], update["generation"])
        self.assertEqual(warm["read_files"], 0)
        self.assertEqual(json.loads(self.value("last_delta")), delta)
        (self.root / "new file.py").unlink()
        self.assertEqual(index.synchronize(self.ws)["deleted"], 1)
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])

    def test_git_scope_ignored_nested_binary_and_byte_limit(self):
        write(self.root, "vendor/first.py", "def embedded(): pass\n")
        write(self.root, "tracked.log", "still tracked\n")
        write(self.root, "moved_out.py", "temporary\n")
        commit(self.root, "tracks")
        (self.root / ".gitignore").write_text("*.log\n/.upstream/\n", encoding="utf-8")
        write(self.root, "skip.log", "ignored\n")
        write(self.root, ".upstream/not-indexed.py", "outside()\n")
        write(self.root, "a/.upstream/stored.py", "normal_project_source()\n")
        new_repo(self.root / "untracked-repo")
        write(self.root, "untracked-repo/secret.py", "outside()\n")
        (self.root / "binary.dat").write_bytes(b"\x00\xffbinary")
        (self.root / "other.dat").write_bytes(b"\xff\xff")
        (self.root / "big.txt").write_bytes(b"A" * (index.MAX_FILE_BYTES + 1))
        update = index.synchronize(self.ws)
        self.assertEqual(update["skipped"], 3)
        self.assertEqual(
            {r[0] for r in self.rows("SELECT path FROM files")},
            {
                "tracked.log",
                "vendor/first.py",
                "moved_out.py",
                ".gitignore",
                "a/.upstream/stored.py",
            },
        )
        self.assertEqual(index.synchronize(self.ws)["read_files"], 0)
        git(self.root / "vendor", "init", "-q")
        # This file stays on disk, but becomes ignored by the selected worktree.
        git(self.root, "rm", "--cached", "moved_out.py")
        write(self.root, ".gitignore", "*.log\n/.upstream/\nmoved_out.py\n")
        self.assertEqual(index.synchronize(self.ws)["deleted"], 2)
        self.assertEqual(
            self.rows("SELECT path FROM files WHERE path LIKE '%first%'"), []
        )
        delta_text = self.value("last_delta")
        self.assertNotIn("embedded", delta_text)
        self.assertNotIn("moved_out.py", delta_text)
        (self.root / "other.dat").write_bytes(b"\xef\xbb\xbfnow text")
        self.assertEqual(index.synchronize(self.ws)["added"], 1)
        self.assertEqual(
            self.rows("SELECT body FROM files WHERE path='other.dat'"), [("now text",)]
        )

    def test_same_size_edit_with_restored_mtime(self):
        path = write(self.root, "unstaged.py", "def first(): pass\n")
        index.synchronize(self.ws)
        before = path.stat()
        write(self.root, "unstaged.py", "def third(): pass\n")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(index.synchronize(self.ws)["updated"], 1)
        self.assertEqual(self.rows("SELECT name FROM symbols"), [("third",)])
        self.assertEqual(index.synchronize(self.ws)["read_files"], 0)

    def test_bom_unicode_sources_and_raw_byte_evidence(self):
        text = "def 信号():\n    return '汉'\n"
        expected = []
        for encoding, bom in (
            ("utf-16-le", codecs.BOM_UTF16_LE),
            ("utf-16-be", codecs.BOM_UTF16_BE),
            ("utf-32-le", codecs.BOM_UTF32_LE),
            ("utf-32-be", codecs.BOM_UTF32_BE),
        ):
            raw = bom + text.encode(encoding)
            name = encoding + ".py"
            (self.root / name).write_bytes(raw)
            expected.append((name, text, hashlib.sha256(raw).hexdigest()))
            self.assertIsNone(index.decode_source(bom + "x\x00y".encode(encoding)))
            self.assertIsNone(index.decode_source(bom + "x\x7fy".encode(encoding)))
        self.assertIsNone(index.decode_source("plain".encode("utf-16-le")))
        self.assertIsNone(index.decode_source(codecs.BOM_UTF16_LE + b"\x00\xd8"))
        self.assertEqual(index.synchronize(self.ws)["added"], 4)
        self.assertEqual(
            self.rows("SELECT path,body,digest FROM files ORDER BY path"),
            sorted(expected),
        )
        self.assertEqual(
            self.rows("SELECT count(*) FROM symbols WHERE name='信号'"), [(4,)]
        )

    def test_symlink_or_junction_parent_cannot_export_other_repo_content(self):
        write(self.root, "inside/tracked.py", "from_root()\n")
        commit(self.root, "tracked")
        index.synchronize(self.ws)
        target = new_repo(self.home / "other")
        write(target, "tracked.py", "outside_private()\n")
        (self.root / "inside" / "tracked.py").unlink()
        (self.root / "inside").rmdir()
        link = self.root / "inside"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("Directory symlinks are unavailable")
            from _winapi import CreateJunction

            CreateJunction(str(target), str(link))
        self.assertEqual(index.synchronize(self.ws)["deleted"], 1)
        self.assertEqual(self.rows("SELECT * FROM files"), [])
        self.assertNotIn("tracked.py", self.value("last_delta"))

    def test_checkout_pull_and_worktree_database_isolation(self):
        write(self.root, "core.py", "def before(): pass\n")
        first = commit(self.root, "old")
        first_update = index.synchronize(self.ws)
        first_id = self.rows("SELECT id FROM symbols")[0][0]
        peer = self.home / "linked"
        git(self.root, "worktree", "add", "-q", "-b", "future", peer)
        other_ws = index.discover(peer)
        self.assertNotEqual(self.ws.gitdir, other_ws.gitdir)
        self.assertEqual(other_ws.database.parent.parent, other_ws.gitdir)
        index.synchronize(other_ws)
        with closing(connect(other_ws)) as db:
            self.assertNotEqual(
                db.execute("SELECT id FROM symbols").fetchone()[0], first_id
            )
        write(peer, "core.py", "def newer(): pass\n")
        write(peer, "additional.py", "def additional(): pass\n")
        second = commit(peer, "new")
        git(
            self.root,
            "-c",
            "protocol.file.allow=always",
            "pull",
            "-q",
            "--ff-only",
            ".",
            "future",
        )
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        update = index.synchronize(self.ws)
        self.assertGreater(update["generation"], first_update["generation"])
        self.assertEqual((update["added"], update["updated"]), (1, 1))
        self.assertEqual(self.rows("SELECT revision FROM repositories"), [(second,)])
        git(self.root, "checkout", "-q", "--detach", first)
        back = index.synchronize(self.ws)
        self.assertEqual((back["updated"], back["deleted"]), (1, 1))
        self.assertEqual(
            self.rows("SELECT id,name FROM symbols"), [(first_id, "before")]
        )
        # A fresh database in a different worktree retains its own unmodified generation.
        with closing(connect(other_ws)) as db:
            self.assertEqual(
                db.execute("SELECT name FROM symbols").fetchone()[0], "before"
            )

    def test_pins_immutable_unpinned_missing_and_scope_removal(self):
        shared = new_repo(self.home / "shared")
        write(shared, "common.py", "def v_one(): pass\n")
        v1 = commit(shared, "shared v1")
        write(shared, "common.py", "def v_two(): pass\n")
        v2 = commit(shared, "shared v2")
        write(shared, "common.py", "def local_uncommitted(): pass\n")
        write(self.root, "main.py", "def business(): pass\n")
        write(self.root, ".execarchify.json", json.dumps({"pins": {"../shared": v1}}))
        index.synchronize(self.ws)
        self.assertEqual(self.rows("SELECT id FROM repositories"), [("root",)])
        index.configure(self.ws, ["../shared", str(shared)])
        update = index.synchronize(self.ws)
        self.assertEqual(update["gaps"], [])
        shared_id, binding, rev = self.rows(
            "SELECT id,binding,revision FROM repositories WHERE role='shared'"
        )[0]
        self.assertEqual((binding, rev), ("pinned", v1))
        self.assertEqual(
            self.rows(
                "SELECT name FROM symbols WHERE file_id IN "
                "(SELECT id FROM files WHERE repo=?)",
                shared_id,
            ),
            [("v_one",)],
        )
        self.assertEqual(git(shared, "rev-parse", "HEAD"), v2)
        self.assertEqual(
            (shared / "common.py").read_text(), "def local_uncommitted(): pass\n"
        )
        index.configure(self.ws)
        with patch.object(
            index, "blob_reader", side_effect=AssertionError("warm shared read")
        ):
            self.assertEqual(
                index.synchronize(self.ws)["generation"], update["generation"]
            )
        (self.root / ".execarchify.json").unlink()
        self.assertTrue(index.synchronize(self.ws)["gaps"])
        self.assertEqual(
            self.rows(
                "SELECT binding,revision FROM repositories WHERE id=?", shared_id
            ),
            [("reference", v2)],
        )
        self.assertIn(("v_two",), self.rows("SELECT name FROM symbols"))
        self.assertNotIn("v_one", self.value("last_delta"))
        bad = "0" * 40
        write(self.root, ".execarchify.json", json.dumps({"pins": {"../shared": bad}}))
        self.assertTrue(index.synchronize(self.ws)["gaps"])
        self.assertEqual(
            self.rows(
                "SELECT binding,revision FROM repositories WHERE id=?", shared_id
            ),
            [("missing", bad)],
        )
        self.assertEqual(self.rows("SELECT id FROM files WHERE repo=?", shared_id), [])
        self.assertNotIn(("v_two",), self.rows("SELECT name FROM symbols"))
        write(self.root, ".execarchify.json", '{"pins": {')
        self.assertTrue(index.synchronize(self.ws)["gaps"])
        self.assertEqual(
            self.rows("SELECT binding FROM repositories WHERE id=?", shared_id),
            [("missing",)],
        )
        index.configure(self.ws, [])
        clean = index.synchronize(self.ws)
        self.assertEqual(clean["gaps"], [])
        self.assertEqual(self.rows("SELECT id FROM repositories"), [("root",)])
        self.assertNotIn(shared_id, self.value("last_delta"))
        self.assertIn(("business",), self.rows("SELECT name FROM symbols"))

    def test_gitlink_uses_superproject_revision_and_rejects_conflicting_lock(self):
        shared = new_repo(self.home / "origin")
        write(shared, "driver.py", "def expected(): pass\n")
        pinned = commit(shared, "v1")
        write(self.root, "main.py", "run()\n")
        git(
            self.root,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            shared,
            "vendor/common",
        )
        commit(self.root, "lock submodule")
        nested = self.root / "vendor" / "common"
        write(nested, "driver.py", "def wrong_checkout(): pass\n")
        git(
            nested,
            "-c",
            "user.name=Index",
            "-c",
            "user.email=x@y.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qam",
            "separate submodule change",
        )
        other = git(nested, "rev-parse", "HEAD")
        index.synchronize(self.ws)
        self.assertEqual(
            self.rows("SELECT path FROM files WHERE path LIKE 'vendor/%'"), []
        )
        index.configure(self.ws, ["vendor/common"])
        self.assertEqual(index.synchronize(self.ws)["gaps"], [])
        self.assertEqual(
            self.rows("SELECT binding,revision FROM repositories WHERE role='shared'"),
            [("pinned", pinned)],
        )
        self.assertIn(("expected",), self.rows("SELECT name FROM symbols"))
        self.assertNotIn(("wrong_checkout",), self.rows("SELECT name FROM symbols"))
        write(
            self.root,
            ".execarchify.json",
            json.dumps({"pins": {"vendor/common": other}}),
        )
        self.assertTrue(index.synchronize(self.ws)["gaps"])
        self.assertEqual(
            self.rows("SELECT binding FROM repositories WHERE role='shared'"),
            [("missing",)],
        )
        self.assertNotIn(("expected",), self.rows("SELECT name FROM symbols"))
        self.assertEqual(git(nested, "rev-parse", "HEAD"), other)
        # An absent submodule or explicit non-repository directory cannot resolve via a parent.
        index.configure(self.ws, ["vendor", "absent"])
        self.assertEqual(len(index.synchronize(self.ws)["gaps"]), 2)
        self.assertEqual(
            self.rows("SELECT count(*) FROM repositories WHERE binding='missing'"),
            [(2,)],
        )

    def test_failure_rolls_back_and_concurrent_synchronizations_publish_once(self):
        write(self.root, "test.py", "def first(): pass\n")
        workers = 4
        barrier = threading.Barrier(workers)

        def start(_):
            barrier.wait(timeout=10)
            return index.synchronize(self.ws)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            updates = list(pool.map(start, range(workers)))
        self.assertEqual(sum(update["added"] for update in updates), 1)
        self.assertEqual({update["generation"] for update in updates}, {1})
        write(self.root, "test.py", "def second(): pass\n")
        write(self.root, "a.py", "def another(): pass\n")
        actual_extract = index.extract

        def fail_after_first(path, source):
            if path == "test.py":
                raise RuntimeError("test parser failure")
            return actual_extract(path, source)

        with (
            patch.object(index, "extract", side_effect=fail_after_first),
            self.assertRaisesRegex(RuntimeError, "test parser failure"),
        ):
            index.synchronize(self.ws)
        self.assertEqual(self.value("generation"), "1")
        self.assertEqual(self.rows("SELECT name FROM symbols"), [("first",)])
        with ThreadPoolExecutor(max_workers=workers) as pool:
            updates = list(pool.map(start, range(workers)))
        self.assertEqual(sum(update["updated"] for update in updates), 1)
        self.assertEqual(sum(update["added"] for update in updates), 1)
        self.assertEqual({update["generation"] for update in updates}, {2})
        self.assertEqual(self.rows("PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(self.rows("SELECT count(*) FROM files_fts"), [(2,)])
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])

    def test_unversioned_facts_rebuilt_including_unchanged_shared_snapshot(self):
        shared = new_repo(self.home / "formats")
        write(shared, "format.py", "def imported(): pass\n")
        expected = commit(shared, "formats")
        write(self.root, "main.py", "def local():\n    imported()\n")
        write(
            self.root,
            ".execarchify.json",
            json.dumps({"pins": {"../formats": expected}}),
        )
        index.configure(self.ws, ["../formats"])
        initial = index.synchronize(self.ws)
        before = self.rows(
            "SELECT id,path,revision,binding FROM repositories ORDER BY id"
        )
        with closing(connect(self.ws)) as db:
            db.execute("DELETE FROM meta WHERE key='facts_version'")
            db.execute("UPDATE symbols SET name='obsolete'")
            db.execute("UPDATE refs SET target='obsolete'")
            db.commit()
        with (
            patch.object(index, "extract", side_effect=RuntimeError("upgrade failure")),
            self.assertRaisesRegex(RuntimeError, "upgrade failure"),
        ):
            index.synchronize(self.ws)
        self.assertEqual(int(self.value("generation")), initial["generation"])
        self.assertEqual(
            self.rows("SELECT value FROM meta WHERE key='facts_version'"), []
        )
        self.assertIn(("obsolete",), self.rows("SELECT name FROM symbols"))
        update = index.synchronize(self.ws)
        self.assertEqual(update["generation"], initial["generation"] + 1)
        self.assertEqual(update["rebuilt"], 3)
        self.assertEqual(self.value("facts_version"), index.FACTS_VERSION)
        self.assertEqual(
            self.rows("SELECT id,path,revision,binding FROM repositories ORDER BY id"),
            before,
        )
        self.assertIn(("local",), self.rows("SELECT name FROM symbols"))
        self.assertIn(("imported",), self.rows("SELECT name FROM symbols"))
        self.assertIn(("imported",), self.rows("SELECT target FROM refs"))
        self.assertNotIn(("obsolete",), self.rows("SELECT name FROM symbols"))
        self.assertNotIn(("obsolete",), self.rows("SELECT target FROM refs"))
        self.assertEqual(index.synchronize(self.ws)["read_files"], 0)


if __name__ == "__main__":
    unittest.main()
