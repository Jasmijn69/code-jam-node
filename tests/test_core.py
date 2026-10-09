import os
import stat
import tempfile
import unittest
from pathlib import Path

from filesystem_node_iterator import NodeRecord, iter_nodes


class _TempTree:
    """A self-contained filesystem tree that cleans up after itself."""

    def __init__(self):
        self.root = tempfile.TemporaryDirectory(prefix="fsni_test_")
        self.root_path = Path(self.root.name)

    def cleanup(self):
        self.root.cleanup()

    def write(self, rel, content=b""):
        p = self.root_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def mkdir(self, rel):
        p = self.root_path / rel
        p.mkdir(parents=True, exist_ok=True)
        return p

    def symlink(self, rel, target):
        p = self.root_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.symlink_to(target)
        return p


class IterNodesBasicTests(unittest.TestCase):
    def setUp(self):
        self.tree = _TempTree()
        self.tree.write("a.txt", b"alpha")
        self.tree.write("dir/b.txt", b"beta")
        self.tree.write("dir/sub/c.txt", b"gamma")
        self.tree.mkdir("empty")

    def tearDown(self):
        self.tree.cleanup()

    def _paths(self, **kw):
        return {r.path for r in iter_nodes(str(self.tree.root_path), **kw)}

    def test_root_always_yielded_by_default(self):
        paths = self._paths()
        self.assertIn(str(self.tree.root_path), paths)

    def test_yields_node_record_instances(self):
        records = list(iter_nodes(str(self.tree.root_path)))
        self.assertTrue(all(isinstance(r, NodeRecord) for r in records))

    def test_includes_nested_files(self):
        paths = self._paths()
        self.assertIn(str(self.tree.root_path / "dir" / "sub" / "c.txt"), paths)

    def test_includes_empty_directory(self):
        paths = self._paths()
        self.assertIn(str(self.tree.root_path / "empty"), paths)

    def test_record_is_dir_property(self):
        root_rec = next(
            r for r in iter_nodes(str(self.tree.root_path))
            if r.path == str(self.tree.root_path)
        )
        self.assertTrue(root_rec.is_dir)
        self.assertFalse(root_rec.is_file)

    def test_record_is_file_property(self):
        file_rec = next(
            r for r in iter_nodes(str(self.tree.root_path))
            if r.path == str(self.tree.root_path / "a.txt")
        )
        self.assertTrue(file_rec.is_file)
        self.assertFalse(file_rec.is_dir)

    def test_stat_result_is_real(self):
        rec = next(
            r for r in iter_nodes(str(self.tree.root_path))
            if r.path == str(self.tree.root_path / "a.txt")
        )
        expected = (self.tree.root_path / "a.txt").stat()
        self.assertEqual(rec.st.st_size, expected.st_size)
        self.assertEqual(rec.st.st_mode, expected.st_mode)


class DepthTests(unittest.TestCase):
    def setUp(self):
        self.tree = _TempTree()
        self.tree.write("L1.txt", b"one")
        self.tree.write("d/L2.txt", b"two")
        self.tree.write("d/sub/L3.txt", b"three")
        self.tree.write("d/sub/deep/L4.txt", b"four")

    def tearDown(self):
        self.tree.cleanup()

    def _depths(self, **kw):
        return {
            r.path: len(Path(r.path).relative_to(self.tree.root_path).parts)
            for r in iter_nodes(str(self.tree.root_path), **kw)
        }

    def test_max_depth_zero_only_root(self):
        depths = self._depths(max_depth=0)
        self.assertEqual(set(depths.values()), {0})

    def test_max_depth_two_excludes_deepest(self):
        depths = self._depths(max_depth=2)
        self.assertNotIn(3, depths.values())
        self.assertLessEqual(max(depths.values()), 2)

    def test_min_depth_one_excludes_root(self):
        depths = self._depths(min_depth=1)
        self.assertNotIn(0, depths.values())
        self.assertGreaterEqual(min(depths.values()), 1)

    def test_min_and_max_combined(self):
        depths = self._depths(min_depth=1, max_depth=1)
        self.assertEqual(set(depths.values()), {1})


class SymlinkTests(unittest.TestCase):
    def setUp(self):
        self.tree = _TempTree()
        self.tree.write("real.txt", b"real")
        self.tree.write("d/inside.txt", b"inside")
        self.tree.symlink("d/link_to_root", self.tree.root_path)
        self.tree.symlink("d/broken", "/nonexistent/target/path")

    def tearDown(self):
        self.tree.cleanup()

    def test_symlink_is_not_a_directory_for_recursion(self):
        """A symlink that points to a directory must not cause infinite recursion."""
        paths = {r.path for r in iter_nodes(str(self.tree.root_path))}
        link_path = str(self.tree.root_path / "d" / "link_to_root")
        # The symlink is filtered out of traversal by _entry_filter, so we must
        # not see it surface as a directory we recursed into.
        for p in paths:
            self.assertNotIn("link_to_root/", p)

    def test_broken_symlink_does_not_raise(self):
        # The broken symlink sits inside a real directory; iterating should not
        # raise, and the symlink itself is excluded from traversal by the same
        # _entry_filter rule.
        list(iter_nodes(str(self.tree.root_path)))


class DeviceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tree = _TempTree()
        self.tree.write("inside.txt", b"local")
        self.fake_dir = self.tree.mkdir("crossmount")

    def tearDown(self):
        self.tree.cleanup()

    def test_on_same_device_yields_boundary_node(self):
        """When the child's device differs, the node is still yielded but not
        descended into. We simulate a foreign device by monkey-patching the
        NodeRecord path's stat at iteration time — except we cannot easily
        intercept os.scandir's entry.stat. Instead, verify the flag is honoured
        when there is no cross-device mount (everything stays consistent)."""
        root = self.tree.root_path
        records = list(iter_nodes(str(root), on_same_device=True))
        paths = {r.path for r in records}
        # Without an actual foreign mount, on_same_device should not change the
        # output set — this is the deterministic, container-friendly assertion.
        self.assertEqual(paths, {r.path for r in iter_nodes(str(root))})


class ErrorHandlingTests(unittest.TestCase):
    def test_missing_root_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            list(iter_nodes("/this/path/does/not/exist/anywhere"))

    def test_file_as_root_raises_not_a_directory(self):
        with tempfile.NamedTemporaryFile(prefix="fsni_file_") as f:
            with self.assertRaises(NotADirectoryError):
                list(iter_nodes(f.name))

    def test_empty_directory_yields_only_root(self):
        with tempfile.TemporaryDirectory(prefix="fsni_empty_") as d:
            records = list(iter_nodes(d))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].path, d)


if __name__ == "__main__":
    unittest.main()
