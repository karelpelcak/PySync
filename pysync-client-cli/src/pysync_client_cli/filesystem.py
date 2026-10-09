import hashlib
import os
import stat
from pathlib import Path
from uuid import uuid4

import pathspec
from pysync_shared.paths import fsync_directory, normalize_path, path_key, read_file


class Scanner:
    def __init__(self, root: Path, patterns: list[str], limit: int):
        self.root = root
        self.spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns + [".pysync-*/"])
        self.limit = limit
        self.directories: set[Path] = set()

    def ignored(self, path: str, directory: bool = False) -> bool:
        if any(part.lower().startswith(".pysync-") for part in path.split("/")):
            return True
        return self.spec.match_file(path + ("/" if directory else ""))

    def list_files(self) -> tuple[dict[str, str], dict[str, str]]:
        """Complete enumeration before inferring deletion; any error disables deletion inference."""
        files, errors, keys = {}, {}, {}
        self.directories = set()

        def walk(descriptor: int, prefix: str) -> None:
            self.directories.add(self.root / prefix)
            try:
                entries = list(os.scandir(descriptor))
            except OSError as error:
                errors[prefix or "."] = str(error)
                return
            for entry in entries:
                raw = prefix + entry.name
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                    if self.ignored(raw, is_directory):
                        continue
                    path = normalize_path(raw)
                    key = path_key(path)
                    if key in keys and keys[key] != raw:
                        errors[raw] = f"Filename collision with {keys[key]}"
                        errors[keys[key]] = f"Filename collision with {raw}"
                        files.pop(path, None)
                        continue
                    keys[key] = raw
                    if entry.is_symlink():
                        errors[raw] = "Symlinks are not synchronized"
                    elif is_directory:
                        child = os.open(
                            entry.name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor,
                        )
                        try:
                            walk(child, raw + "/")
                        finally:
                            os.close(child)
                    elif entry.is_file(follow_symlinks=False):
                        if entry.stat(follow_symlinks=False).st_size > self.limit:
                            errors[raw] = "File exceeds configured size limit"
                        else:
                            files[path] = raw
                    else:
                        errors[raw] = "Special files are not synchronized"
                except (OSError, ValueError) as error:
                    errors[raw] = str(error)

        if self.root.is_symlink() or not self.root.is_dir():
            return {}, {".": "Project root is missing or unsafe"}
        try:
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                walk(descriptor, "")
            finally:
                os.close(descriptor)
        except OSError as error:
            errors["."] = str(error)
        # Remove all members of a colliding set, not just the second entry.
        for path, raw in list(files.items()):
            if raw in errors:
                files.pop(path)
        return files, errors

    def snapshot(
        self, relative: str, spool: Path, expected: tuple[str, bool] | None = None
    ) -> tuple[dict, Path | None]:
        fd, before = read_file(self.root, relative)
        temp = spool / uuid4().hex
        digest, size = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "rb") as inp:
                while chunk := inp.read(256 * 1024):
                    size += len(chunk)
                    if size > self.limit:
                        raise ValueError("File exceeds configured size limit")
                    digest.update(chunk)
                info = {
                    "hash": digest.hexdigest(),
                    "size": size,
                    "mtime": before.st_mtime,
                    "executable": bool(before.st_mode & stat.S_IXUSR),
                }
                if expected != (info["hash"], info["executable"]):
                    inp.seek(0)
                    copied = hashlib.sha256()
                    copied_size = 0
                    with temp.open("xb") as out:
                        while chunk := inp.read(256 * 1024):
                            copied_size += len(chunk)
                            if copied_size > min(self.limit, size):
                                raise OSError("File grew during snapshot; retrying")
                            copied.update(chunk)
                            out.write(chunk)
                        out.flush()
                        os.fsync(out.fileno())
                    if copied.hexdigest() != info["hash"]:
                        raise OSError("File changed during snapshot; retrying")
                after = os.fstat(inp.fileno())
                new_fd, current = read_file(self.root, relative)
                os.close(new_fd)

                def stamp(value: os.stat_result) -> tuple[int, int, int, int]:
                    return value.st_ino, value.st_mtime_ns, value.st_ctime_ns, value.st_size

                if stamp(before) != stamp(after) or stamp(after) != stamp(current):
                    raise OSError("File changed while being read; retrying after debounce")
            if expected == (info["hash"], info["executable"]):
                return info, None
            fsync_directory(spool)
            return info, temp
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
