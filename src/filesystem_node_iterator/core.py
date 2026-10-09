from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Union


@dataclass(frozen=True)
class NodeRecord:
    """A single filesystem entry together with the stat data we bothered to read.

    We expose the full ``st`` result because the caller usually wants more than
    just the type — sizes and mtimes are cheap to already have in hand. We do
    not re-stat; callers wanting fresher data must do it themselves.
    """

    path: str
    st: os.stat_result

    @property
    def is_dir(self) -> bool:
        return stat.S_ISDIR(self.st.st_mode)

    @property
    def is_file(self) -> bool:
        return stat.S_ISREG(self.st.st_mode)

    @property
    def is_symlink(self) -> bool:
        return stat.S_ISLNK(self.st.st_mode)


def _should_recurse(entry: os.DirEntry) -> bool:
    """Decide whether an entry should be pushed onto the stack for visiting.

    Symlinks are excluded so that directory recursion does not follow them —
    a symlink to a directory above must not cause infinite recursion. Files
    are included so that they are yielded; the ``S_ISDIR`` check during
    recursion prevents descending into them.
    """
    if entry.is_symlink():
        return False
    return True


def iter_nodes(
    root: Union[str, "os.PathLike[str]"],
    *,
    min_depth: int = 0,
    max_depth: Optional[int] = None,
    on_same_device: bool = False,
) -> Iterator[NodeRecord]:
    """Lazily yield every filesystem entry under ``root`` as a ``NodeRecord``.

    Traversal order within a directory is whatever the OS gave us via
    ``os.scandir`` — we do not sort. Use this when you want to walk a tree once
    and don't care about ordering. If you need a stable order, sort the results
    downstream; doing it here would force materialisation and defeat the point
    of a generator.

    Args:
        root: Directory to start from. Must exist and be a directory.
        min_depth: Skip entries shallower than this. ``depth=0`` is ``root``
            itself, so ``min_depth=1`` excludes ``root`` from the output.
        max_depth: Stop descending past this depth. ``None`` means unlimited.
            Entries *at* ``max_depth`` are still yielded; their subdirectories
            are not descended into.
        on_same_device: If True, do not cross into a different filesystem. A
            subdirectory whose ``st_dev`` differs from ``root``'s is yielded
            (so the caller sees it) but not recursed into.

    Yields:
        NodeRecord: one per visited filesystem entry, depth-first.

    The root itself is always yielded first (subject to ``min_depth``) before
    its children, because knowing the parent's stat before processing children
    is the common case.
    """
    root_path = Path(root)

    # Fail loudly on a nonsensical starting point rather than silently producing
    # an empty iterator. A missing root is almost always a configuration bug.
    if not root_path.exists():
        raise FileNotFoundError(f"root does not exist: {root_path}")
    if not root_path.is_dir():
        raise NotADirectoryError(f"root is not a directory: {root_path}")

    # We stat root eagerly because we need its device id even when min_depth > 0.
    root_st = root_path.stat()
    root_device = root_st.st_dev

    stack: list[tuple[Path, int, bool]] = [(root_path, 0, True)]

    while stack:
        current, depth, can_recurse = stack.pop()

        try:
            st = current.stat()
        except OSError:
            # Something under us deleted or moved the entry between the readdir
            # and now. Skip it; we cannot meaningfully report a node we cannot
            # stat. This is the normal TOCTOU cost of walking a live filesystem.
            continue

        if depth >= min_depth:
            yield NodeRecord(str(current), st)

        if not can_recurse:
            continue
        if max_depth is not None and depth >= max_depth:
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue

        # os.scandir outperforms os.listdir + stat on every common platform
        # because the directory read already populated most of the stat fields.
        try:
            entries = list(os.scandir(current))
        except OSError:
            # Permission denied or the directory vanished. We have already
            # yielded the node itself; we just cannot descend into it.
            continue

        # Push in reverse so that a stable-input tree is visited in forward
        # order. This does NOT impose a sorted order on the caller — it only
        # makes iteration order match the readdir order, which is generally
        # more pleasant to consume than reversed.
        for entry in reversed(entries):
            if not _should_recurse(entry):
                continue
            child_path = Path(entry.path)
            child_can_recurse = True
            if on_same_device:
                # We resolve the child's device via lstat so that a symlink
                # whose target is on a different filesystem doesn't trick us.
                # _should_recurse already discarded symlinks, but the device
                # check is still defensive against unexpected special files.
                try:
                    child_st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if child_st.st_dev != root_device:
                    # The boundary node is still yielded (so the caller can log
                    # it), we just refuse to step across it.
                    child_can_recurse = False
            stack.append((child_path, depth + 1, child_can_recurse))
