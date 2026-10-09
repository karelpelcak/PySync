"""Portable paths and descriptor-relative operations; symlinks are never followed."""

import hashlib
import os
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class UnsafePath(ValueError):
    pass


def normalize_path(value: str) -> str:
    if not value or len(value.encode("utf-8")) > 1024 or "\\" in value:
        raise UnsafePath("Path must be a short POSIX project-relative path")
    parts = value.split("/")
    for part in parts:
        if (
            part in ("", ".", "..")
            or any(ord(c) < 32 or c in ':<>"|?*' for c in part)
            or part.endswith((" ", "."))
            or len(part.encode("utf-8")) > 240
            or part.lower().startswith(".pysync-")
        ):
            raise UnsafePath(f"Unsafe or reserved path component: {part!r}")
    return unicodedata.normalize("NFC", value)


def path_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


@contextmanager
def parent_fd(root: Path, relative: str, create: bool = False) -> Iterator[tuple[int, str]]:
    """Hold parent directory descriptors to defeat symlink swaps during an operation."""
    normalize_path(relative)
    parts = relative.split("/")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in parts[:-1]:
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def read_file(root: Path, relative: str) -> tuple[int, os.stat_result]:
    with parent_fd(root, relative) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise UnsafePath("Only regular files can be synchronized")
    return fd, info


def fingerprint(root: Path, relative: str) -> tuple[str, bool] | None:
    try:
        fd, info = read_file(root, relative)
    except FileNotFoundError:
        return None
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as stream:
        while chunk := stream.read(256 * 1024):
            digest.update(chunk)
    return digest.hexdigest(), bool(info.st_mode & stat.S_IXUSR)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def apply_file(
    root: Path,
    relative: str,
    source: Path | None,
    executable: bool,
    recovery_name: str | None = None,
) -> Path | None:
    """Preserve old inode in recovery, then publish with exclusive link.

    A concurrent editor recreating the path wins: publication fails with EEXIST.
    Interrupted replacements leave the previous content in .pysync-recovery.
    All recoveries are retained until the user explicitly removes them.
    """
    recovery_name = recovery_name or uuid4().hex
    if not recovery_name.isalnum():
        raise UnsafePath("Invalid internal recovery name")
    backup = None
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.mkdir(".pysync-recovery", 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        recovery_fd = os.open(
            ".pysync-recovery", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
        )
        try:
            with parent_fd(root, relative, create=source is not None) as (parent, name):
                temp = f".pysync-{uuid4().hex}"
                try:
                    if source is not None:
                        dest = os.open(
                            temp,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o755 if executable else 0o644,
                            dir_fd=parent,
                        )
                        with os.fdopen(dest, "wb") as out, source.open("rb") as inp:
                            while chunk := inp.read(256 * 1024):
                                out.write(chunk)
                            out.flush()
                            os.fsync(out.fileno())
                    try:
                        existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        if not stat.S_ISREG(existing.st_mode):
                            raise UnsafePath("Destination is a symlink, directory, or special file")
                        os.rename(name, recovery_name, src_dir_fd=parent, dst_dir_fd=recovery_fd)
                        os.fsync(recovery_fd)
                        backup = root / ".pysync-recovery" / recovery_name
                    except FileNotFoundError:
                        pass
                    if source is not None:
                        # Unlike replace(), this cannot overwrite an editor's intervening save.
                        os.link(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
                    os.fsync(parent)
                finally:
                    if source is not None:
                        try:
                            os.unlink(temp, dir_fd=parent)
                        except FileNotFoundError:
                            pass
        finally:
            os.close(recovery_fd)
    finally:
        os.close(root_fd)
    return backup


def paths_collide(first: str, second: str) -> bool:
    """Case-folded aliases and file/directory overlaps are unsafe across platforms."""
    if first == second:
        return False
    a, b = first.split("/"), second.split("/")
    for left, right in zip(a, b, strict=False):
        if path_key(left) != path_key(right):
            return False
        if left != right:
            return True
    return True  # Shared full prefix: two aliases, or a file used as a parent directory.
