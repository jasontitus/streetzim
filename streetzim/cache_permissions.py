"""Preserve cache access when an atomic update creates a new inode.

Linux POSIX ACLs and macOS extended ACLs require the original owner/group:
rewriting their identities changes ACL semantics. An unprivileged ACL-only
writer who cannot retain ownership is rejected before publication; such a
cache needs a common writer UID or an appropriately privileged writer.
"""
from __future__ import annotations

import contextlib
import ctypes
import errno
from functools import lru_cache
import os
from pathlib import Path
import sys

_LINUX_ACL = "system.posix_acl_access"
_NO_ATTRIBUTE = {errno.ENODATA, errno.ENOTSUP}
# macOS's "no such attribute"; Linux uses ENODATA and has no ENOATTR.
_ENOATTR = getattr(errno, "ENOATTR", None)
if _ENOATTR is not None:
    _NO_ATTRIBUTE.add(_ENOATTR)


@lru_cache(maxsize=1)
def _darwin_functions():
    library = ctypes.CDLL(None, use_errno=True)
    library.copyfile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                                ctypes.c_uint32]
    library.copyfile.restype = ctypes.c_int
    library.acl_get_file.argtypes = [ctypes.c_char_p, ctypes.c_int]
    library.acl_get_file.restype = ctypes.c_void_p
    library.acl_init.argtypes = [ctypes.c_int]
    library.acl_init.restype = ctypes.c_void_p
    library.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    library.acl_set_file.restype = ctypes.c_int
    library.acl_free.argtypes = [ctypes.c_void_p]
    library.acl_free.restype = ctypes.c_int
    return library


def _darwin_acl(source: Path, destination: Path | None = None) -> bool:
    library = _darwin_functions()
    if destination is None:
        # SDK copyfile.h: COPYFILE_ACL=1, COPYFILE_CHECK=1<<16.
        result = library.copyfile(os.fsencode(source), None, None, 1 | (1 << 16))
        error = ctypes.get_errno()
    else:
        # copyfile's ACL copy retains inherited destination entries. Native
        # acl_set_file replaces the entire ACL, including an empty source.
        # SDK sys/acl.h: ACL_TYPE_EXTENDED=0x100.
        acl = (library.acl_get_file(os.fsencode(source), 0x100)
               if _darwin_acl(source) else library.acl_init(0))
        if not acl:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(source))
        try:
            result = library.acl_set_file(os.fsencode(destination), 0x100, acl)
            error = ctypes.get_errno()
        finally:
            library.acl_free(acl)
    if result < 0:
        raise OSError(error, os.strerror(error), str(source))
    return bool(result & 1)


def _linux_acl(path: Path) -> bytes | None:
    if sys.platform != "linux":
        raise OSError(errno.ENOTSUP, "Linux access ACLs require Linux")
    try:
        return os.getxattr(path, _LINUX_ACL)
    except OSError as error:
        if error.errno in _NO_ATTRIBUTE:
            return None
        raise


def prepare_private_stage_directory(path: Path) -> None:
    """Clear an empty staging directory's access ACL and restrict it to its owner.

    Call before creating any child files; chmod alone does not remove named
    ACL grants. A descriptor opened while the directory was empty does not
    bypass the resulting search permissions when opening a later child.
    """
    if sys.platform == "linux":
        try:
            os.removexattr(path, _LINUX_ACL)
        except OSError as error:
            if error.errno not in _NO_ATTRIBUTE:
                raise
    elif sys.platform == "darwin":
        library = _darwin_functions()
        acl = library.acl_init(0)
        if not acl:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(path))
        try:
            result = library.acl_set_file(os.fsencode(path), 0x100, acl)
            error = ctypes.get_errno()
        finally:
            library.acl_free(acl)
        if result < 0:
            raise OSError(error, os.strerror(error), str(path))
    else:
        raise OSError(errno.ENOTSUP, "Private cache staging requires Linux or macOS")
    os.chmod(path, 0o700)


def _group_fixes(gid: int) -> str:
    return ("Run the build as a user in that group (Docker: add "
            f"--group-add {gid}; `--user uid:gid` alone gives no other groups), "
            "give the file a group this user is in (chgrp) or the same access for "
            "the group as for everyone else (chmod), or point the build at a cache "
            "of its own (--wikidata-cache, or --dl for the streetzim command).")


def _acl_fixes(uid: int) -> str:
    return (f"Run the build as UID {uid} (the file's owner) or with the privilege "
            "to keep another user's ownership (root), remove the ACL if it is not "
            "wanted (setfacl -b), or point the build at a cache of its own "
            "(--wikidata-cache, or --dl for the streetzim command).")


def _group(gid: int) -> str:
    try:
        import grp
        return f"{grp.getgrgid(gid).gr_name} (GID {gid})"
    except (ImportError, KeyError):
        return f"GID {gid}"


def _who() -> str:
    """This process's user and groups, for errors about sharing."""
    groups = sorted(set(os.getgroups()) | {os.getegid()})
    return (f"This build (UID {os.geteuid()}, groups "
            f"{', '.join(_group(g) for g in groups)})")


def preserve_cache_permissions(staging: Path, previous: Path) -> None:
    """Copy access metadata before publication, or leave the old file intact.

    Mode/group sharing works without special privilege. Existing ACLs are
    preserved exactly only when the old UID and GID can also be retained.
    This helper never publishes or removes either file; callers own cleanup.
    """
    try:
        original = previous.stat()
    except FileNotFoundError:
        return  # A new file retains the directory's normal inheritance/umask.
    if sys.platform == "linux":
        acl = _linux_acl(previous)
    elif sys.platform == "darwin":
        acl = _darwin_acl(previous)
    else:
        raise OSError(errno.ENOTSUP, "Atomic cache ACL preservation requires Linux or macOS")

    stage = staging.stat()
    mode = original.st_mode & 0o777
    if (stage.st_uid, stage.st_gid) != (original.st_uid, original.st_gid):
        try:
            # Privileged maintenance must not take ownership from the next
            # normal writer, including the newly published lock inode.
            os.chown(staging, original.st_uid, original.st_gid)
        except PermissionError as error:
            if acl:
                raise PermissionError(
                    errno.EACCES,
                    f"{_who()} cannot update {previous} in the shared cache: it has an "
                    f"ACL, which a replacement keeps only with the same owner (UID "
                    f"{original.st_uid}, GID {original.st_gid}), and this user cannot "
                    "give it that owner. Replacing it anyway would change who may read "
                    f"or write it. {_acl_fixes(original.st_uid)} (See docs/zimfarm.md, "
                    "'Shared caches: ownership and permissions'.)") from error
            if stage.st_gid != original.st_gid:
                try:
                    os.chown(staging, -1, original.st_gid)
                except PermissionError as group_error:
                    # Without an ACL these are actual group/other permissions.
                    # An ACL's st_mode group bits would be its mask instead.
                    if ((mode >> 3) & 0o7) != (mode & 0o7):
                        raise PermissionError(
                            errno.EACCES,
                            f"{_who()} cannot update {previous} in the shared cache: "
                            f"it is shared through group {_group(original.st_gid)} "
                            f"(mode {mode:03o}: the group's access differs from "
                            "everyone else's), and this user is not in that group, so "
                            "a replacement could not keep it; publishing one under "
                            "this user's group would change who may write it. "
                            f"{_group_fixes(original.st_gid)} (See docs/zimfarm.md, "
                            "'Shared caches: ownership and permissions'.)"
                        ) from group_error
    os.chmod(staging, mode)
    if sys.platform == "linux":
        if isinstance(acl, bytes):
            os.setxattr(staging, _LINUX_ACL, acl)
        else:
            try:
                os.removexattr(staging, _LINUX_ACL)
            except OSError as error:
                if error.errno not in _NO_ATTRIBUTE:
                    raise
    else:
        _darwin_acl(previous, staging)


def make_shared_dirs(directory: Path | str, root: Path | str) -> None:
    """Create `directory` under the cache `root`, each new level with the
    root's group and mode (share_like); a level another build creates at
    the same moment is fine."""
    directory, root = Path(directory), Path(root)
    rel = directory.relative_to(root)
    cur = root
    for part in rel.parts:
        cur = cur / part
        if not cur.is_dir():
            try:
                cur.mkdir()
            except FileExistsError:
                continue
            share_like(cur, root)


def share_like(directory: Path, like: Path) -> None:
    """Give a new cache subdirectory the group and mode of the cache root it
    sits in (a per-language Wikidata cache under a shared root), so the
    builds that share the root share it too. Best effort: a group this
    user cannot set is left as created."""
    st = like.stat()
    with contextlib.suppress(PermissionError):
        if directory.stat().st_gid != st.st_gid:
            os.chown(directory, -1, st.st_gid)
    with contextlib.suppress(PermissionError):
        os.chmod(directory, st.st_mode & 0o7777)
