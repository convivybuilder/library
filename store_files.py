"""Create the Library's store files owner-only, whatever the caller's umask.

`serve.sh` refuses to start when `LIBRARY_DB`, `LIBRARY_COLD_DB` or
`LIBRARY_VECTORS_DB` is accessible by group or other (library#317). SQLite
creates a missing database at 0666 less the process umask, so a fresh install
at the common default umask of 022 made all three at 0644, and the service it
had just installed refused them (Shipwright's Debian 12 rehearsal of the 0.7.0
install, at library 93ba6d0d).

So every path that can create one of those files calls `create_private`
first, and SQLite then opens a file that already exists at 0600. SQLite gives
its `-journal`, `-wal` and `-shm` files the database file's own mode, so they
follow. An existing file keeps its mode: changing a live store's mode is the
operator's act, and `serve.sh` already refuses one that is too open.

The callers are alembic's online run and its cold `ATTACH`, `init_cold`,
`init_vectors`, and the writable `connect` in `migrate` and `split_vectors`,
whose outputs become the live `LIBRARY_DB` and `LIBRARY_VECTORS_DB`.

Another account cannot race this: the state directory is 0700, which is what
keeps anyone else from placing a file or a symlink at the path first. Within
the owner's own account, `O_EXCL` refuses a symlink at the path rather than
following it.
"""
from __future__ import annotations

import os
from pathlib import Path


def create_private(path: Path | str) -> bool:
    """Create `path` empty at mode 0600 when nothing is there. Returns True
    when this call created it.

    `O_EXCL` makes creation and the existence check one step, so a file
    another process made in between is left alone rather than truncated.
    The umask can only remove bits from the 0600 passed to `os.open`, so the
    file never exists at a wider mode. The `chmod` restores an owner bit an
    unusual umask removed, and the read-back refuses rather than leaving a
    store file at any other mode, the create-then-verify shape
    `generate_csr.py` uses for its private key.
    """
    path = Path(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    os.close(fd)
    os.chmod(path, 0o600)
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise OSError(f"{path} was created at mode {oct(mode)}, not 0o600")
    return True
