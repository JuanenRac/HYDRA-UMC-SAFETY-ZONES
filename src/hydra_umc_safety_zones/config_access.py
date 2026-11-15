# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/config_access.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Where the zones file lives and whether the account of the service can read it.

The service runs as its own account, and the zones file is its safety configuration: if the account cannot read it, `serve` refuses to start (it never
falls back to "no zones"). The directory the unit names is therefore one that systemd creates for this service alone (`ConfigurationDirectory=`), so the
shared `/etc/hydra-umc` directory, which only the group of the agent may enter, is never touched. Four small helpers live here:

* `diagnose_access` names the first directory the account cannot enter or the file it cannot read, for the message of a refusal;
* `ensure_directory` makes the directory of the service enterable by its account (root, group of the service, 0750) and leaves a correct one untouched;
* `migrate_legacy_zones` copies the file of an earlier layout into the new directory once, never overwriting and never deleting the old one;
* `check_access` is the check an installer runs as the service account before it stops the running service.
"""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

DEFAULT_ZONES_FILE = "/etc/hydra-umc-safety-zones/zones.json"
LEGACY_ZONES_FILE = "/etc/hydra-umc/safety-zones.zones.json"


def diagnose_access(path: str) -> str:
    """One sentence saying why the current account cannot read `path`, or "" when it can (or when the reason is not the permissions)."""
    target = Path(path)
    parts = [target, *target.parents]
    for directory in reversed(parts[1:]):
        if not directory.exists():
            break  # a directory that is not there is a missing file, said below; it is not a permission
        try:
            entered = os.access(directory, os.X_OK)
        except OSError:
            entered = False
        if not entered:
            try:
                info = directory.stat()
                detail = f"mode {stat.filemode(info.st_mode)}, owner uid {info.st_uid}, group gid {info.st_gid}"
            except OSError:
                detail = "it cannot be inspected"
            return f"the directory {directory} cannot be entered by this account ({detail}); put the zones file in a directory this account can read"
    if target.exists() and not os.access(target, os.R_OK):
        info = target.stat()
        return f"the file {target} cannot be read by this account (mode {stat.filemode(info.st_mode)}, owner uid {info.st_uid}, group gid {info.st_gid})"
    if not target.exists():
        return f"the file {target} does not exist: the zones come from a risk assessment and the service does not start without them"
    return ""


def migrate_legacy_zones(legacy: str, target: str, group: str = "") -> str:
    """Copy the zones file (and its signature, if any) from the old place to `target` once. Returns what was done, in a word:
    `kept` (target already there), `none` (nothing to migrate) or `migrated`. Never overwrites, never deletes the old file."""
    destination = Path(target)
    if destination.exists():
        return "kept"
    source = Path(legacy)
    if not source.is_file():
        return "none"
    destination.parent.mkdir(parents=True, exist_ok=True)
    for old, new in ((source, destination), (Path(f"{legacy}.sig"), Path(f"{target}.sig"))):
        if not old.is_file() or new.exists():
            continue
        shutil.copyfile(old, new)
        os.chmod(new, 0o640)
        if group:
            shutil.chown(new, user="root", group=group)
    return "migrated"


def _ownership(path: Path) -> tuple[int, int, int]:
    """(permission bits, owner uid, group gid) of a directory."""
    info = os.stat(path)
    return stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid


def ensure_directory(directory: str, group: str = "") -> str:
    """Make the directory of the service (and only that one) mode 0750, owned by root with the group of the service, so the account of the service can
    enter it whichever way systemd created it. Returns `kept` (it was already right: nothing is touched), `created` or `fixed`. Only root can change an
    owner: any other account creates a missing directory and leaves an existing one as it is (`check-access` then says whether it is usable).
    The shared `/etc/hydra-umc` is refused."""
    path = Path(directory)
    if path.name == "hydra-umc":
        raise ValueError(f"{directory} is the shared directory: it is never changed")
    created = not path.is_dir()
    if created:
        path.mkdir(parents=True, mode=0o750)
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    if not is_root:
        return "created" if created else "kept"
    wanted_gid = None
    if group:
        import grp  # noqa: PLC0415 - POSIX only; the service itself runs on Linux

        wanted_gid = grp.getgrnam(group).gr_gid  # type: ignore[attr-defined,unused-ignore]
    mode, uid, gid = _ownership(path)
    if not created and mode == 0o750 and uid == 0 and (wanted_gid is None or gid == wanted_gid):
        return "kept"
    os.chmod(path, 0o750)
    if group:
        shutil.chown(path, user="root", group=group)
    return "created" if created else "fixed"


def group_exists(name: str) -> bool:
    try:
        import grp  # noqa: PLC0415 - POSIX only; the service itself runs on Linux

        grp.getgrnam(name)  # type: ignore[attr-defined,unused-ignore]
    except ImportError:
        return False
    except KeyError:
        return False
    return True


def check_access(path: str) -> tuple[int, str]:
    """(exit code, message): 0 when this account can open and read `path`, 2 with the reason when it cannot."""
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError as exc:
        reason = diagnose_access(path) or f"{type(exc).__name__}: {exc}"
        return 2, f"[safety-zones] cannot read the zones file {path}: {reason}"
    return 0, f"[safety-zones] the zones file {path} is readable by this account"
