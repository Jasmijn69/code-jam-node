# Filesystem Node Iterator

Lazily walks a directory tree and yields every entry as a `NodeRecord` with its `os.stat_result` attached, with optional depth limits and a same-device boundary check.

```python
from filesystem_node_iterator import iter_nodes, NodeRecord

for node in iter_nodes("/var/log", max_depth=2, on_same_device=True):
    print(node.path, node.st.st_size, node.is_dir)
```

`iter_nodes(root, *, min_depth=0, max_depth=None, on_same_device=False)` returns an iterator of `NodeRecord`. Each `NodeRecord` exposes `path` (the absolute path as a string), `st` (the `os.stat_result`), and the convenience properties `is_dir`, `is_file`, `is_symlink`. The root itself is yielded first; entries below follow depth-first in the OS's readdir order, which is not sorted.

## Why this exists

`os.walk` is eager within a level and returns strings, so you end up re-statting every node to get type and size information. `iter_nodes` exists to yield each entry once, with its stat already in hand, as a generator that can be stopped early. It uses `os.scandir` under the hood so the directory read does most of the stat work for free.

The trade-off: no topological sort, no stable order, and no symlink following. If you need a sorted walk, sort the materialised list — doing it inside the generator would defeat laziness.

## Edges you will hit

Symlinks are not descended into, ever. A symlink to a directory above will not cause infinite recursion because it is filtered before the recurse decision. This means a tree that uses symlinks to link libraries (e.g. `/usr/local/lib/foo -> /opt/foo`) will not visit the link's target — the symlink itself is skipped from traversal, though its parent directory is still visited.

`on_same_device=True` yields a cross-filesystem subdirectory but does not descend into it. The node appears in the stream so the caller can log the boundary; its children do not. This is the only meaningful way to handle mount-point traversal lazily without surprising callers who expected a complete subtree.

A node that vanishes between `readdir` and `stat` (TOCTOU) is silently skipped. This is normal when walking a live filesystem; if you need atomic consistency, copy the tree first.
