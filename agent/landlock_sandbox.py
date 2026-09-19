"""Landlock-based filesystem confinement for a profile's terminal commands.

Landlock (Linux Security Module, kernel >= 5.13) lets an UNPRIVILEGED process restrict its own
future filesystem access — no root, no new namespaces, no container. Once applied
(``landlock_restrict_self``) the restriction is irreversible for the calling process and every
descendant, even one that later gains root: this is why it is the mechanism used here instead of
bubblewrap (needs ``unshare(CLONE_NEWUSER)``, blocked by Docker's default seccomp profile) or a
per-agent container (explicitly ruled out — no new containers).

This is a DEFENSE-IN-DEPTH layer on top of the admin-granted ``AgentPermissions`` checks
(``hermes_cli/agent_permissions.py``), not a replacement for them: those already gate which
*operations* an agent's own turn may perform (create/edit skills, manage webhooks, ...). Landlock
adds a kernel-enforced boundary specifically for the terminal tool's ``bash -c "..."`` — the one
execution path that runs arbitrary commands with no per-file check at all
(see ``agent/file_safety.py``'s own admission that "the terminal tool runs as the same OS user and
can read/write anything").

Fail-open by design: on any unsupported platform/kernel this is a silent no-op (logged once) — a
process without Landlock support behaves exactly as it does today. This is deliberate: Landlock is
additive hardening, and refusing to run a command because the kernel lacks a LSM would regress
every non-Linux (or old-kernel) deployment for no security gain (there is no privileged fallback
path that a missing Landlock support would have prevented).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

# -- Landlock ABI (uapi/linux/landlock.h) — no Python stdlib binding exists, so the syscall
# numbers and structs are hand-declared here, ABI-stable since Landlock's kernel introduction.
_SYS_landlock_create_ruleset = 444
_SYS_landlock_add_rule = 445
_SYS_landlock_restrict_self = 446
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_ACCESS_FS_EXECUTE = 1 << 0
_ACCESS_FS_WRITE_FILE = 1 << 1
_ACCESS_FS_READ_FILE = 1 << 2
_ACCESS_FS_READ_DIR = 1 << 3
_ACCESS_FS_REMOVE_DIR = 1 << 4
_ACCESS_FS_REMOVE_FILE = 1 << 5
_ACCESS_FS_MAKE_CHAR = 1 << 6
_ACCESS_FS_MAKE_DIR = 1 << 7
_ACCESS_FS_MAKE_REG = 1 << 8
_ACCESS_FS_MAKE_SOCK = 1 << 9
_ACCESS_FS_MAKE_FIFO = 1 << 10
_ACCESS_FS_MAKE_BLOCK = 1 << 11
_ACCESS_FS_MAKE_SYM = 1 << 12

_ACCESS_FS_READ_ONLY = _ACCESS_FS_EXECUTE | _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR
_ACCESS_FS_READ_WRITE = _ACCESS_FS_READ_ONLY | (
    _ACCESS_FS_WRITE_FILE | _ACCESS_FS_REMOVE_DIR | _ACCESS_FS_REMOVE_FILE |
    _ACCESS_FS_MAKE_CHAR | _ACCESS_FS_MAKE_DIR | _ACCESS_FS_MAKE_REG |
    _ACCESS_FS_MAKE_SOCK | _ACCESS_FS_MAKE_FIFO | _ACCESS_FS_MAKE_BLOCK | _ACCESS_FS_MAKE_SYM)

# Read-only system paths a shell/interpreter needs to function at all. Never writable, never the
# profile's own data — a fixed, minimal allowlist, not derived from the running command.
# /command and /package: s6-overlay's own binaries (s6-setuidgid, etc.) — without these the
# hermes-exec-shim.sh wrapper at /opt/hermes/bin/hermes fails with "command not found" instead of
# running, breaking a permitted agent's ability to use the CLI at all (found live: an agent with
# webhooks.can_manage=true still couldn't run `hermes webhook subscribe` because the shim's own
# privilege-check exec target was outside the sandbox — not a security gap since the denied case
# also failed, but it silently broke the PERMITTED case too).
_READ_ONLY_SYSTEM_PATHS: tuple[str, ...] = (
    "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt/hermes", "/command", "/package",
)
# /tmp is DELIBERATELY excluded: it is shared across every profile in this container (all
# agents run as the same OS user, uid 1000), so granting it read-write turns it into a
# cross-agent covert channel — confirmed live: one agent wrote a file under /tmp and a
# DIFFERENT agent's sandboxed process read it back, despite both being confined to separate
# profile_home directories. Each profile gets its OWN tmp under its own profile_home instead
# (see restrict_to_profile_home's tmp_subdir handling), which is already covered by the
# profile_home READ_WRITE rule — no extra rule needed, just TMPDIR/TMP/TEMP pointed there.
_READ_WRITE_SYSTEM_PATHS: tuple[str, ...] = ("/dev/null", "/dev/urandom", "/dev/zero")


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


_libc: Optional[ctypes.CDLL] = None
_supported: Optional[bool] = None


def _get_libc() -> Optional[ctypes.CDLL]:
    global _libc
    if _libc is None:
        try:
            _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        except OSError:
            return None
    return _libc


def landlock_supported() -> bool:
    """True once, cached: this process can call the Landlock syscalls at all.

    Linux only. Probes with a real (harmless) ``landlock_create_ruleset(NULL, 0, 1)`` call rather
    than trusting a kernel-version heuristic — some distros backport Landlock, some ship kernels
    new enough on paper but with it compiled out, so the syscall itself is the only reliable
    signal. The probe ruleset fd (if created) is closed immediately; nothing is restricted here.
    """
    global _supported
    if _supported is not None:
        return _supported
    if sys.platform != "linux":
        _supported = False
        return False
    libc = _get_libc()
    if libc is None:
        _supported = False
        return False
    try:
        attr = _RulesetAttr(handled_access_fs=_ACCESS_FS_READ_ONLY)
        fd = libc.syscall(_SYS_landlock_create_ruleset, ctypes.byref(attr), ctypes.sizeof(attr), 0)
        if fd < 0:
            _supported = False
        else:
            os.close(fd)
            _supported = True
    except Exception as exc:
        logger.debug("Landlock support probe failed: %s", exc)
        _supported = False
    if not _supported:
        logger.warning(
            "Landlock not available on this kernel/platform — per-agent terminal filesystem "
            "confinement is disabled (falling back to the existing admin-granted permission "
            "checks only). This is expected on non-Linux hosts or kernels older than 5.13.")
    return _supported


def _create_ruleset(libc: ctypes.CDLL) -> int:
    attr = _RulesetAttr(handled_access_fs=_ACCESS_FS_READ_WRITE)
    fd = libc.syscall(_SYS_landlock_create_ruleset, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")
    return fd


_ACCESS_FS_DIRECTORY_ONLY = (
    _ACCESS_FS_READ_DIR | _ACCESS_FS_REMOVE_DIR | _ACCESS_FS_REMOVE_FILE | _ACCESS_FS_MAKE_CHAR |
    _ACCESS_FS_MAKE_DIR | _ACCESS_FS_MAKE_REG | _ACCESS_FS_MAKE_SOCK | _ACCESS_FS_MAKE_FIFO |
    _ACCESS_FS_MAKE_BLOCK | _ACCESS_FS_MAKE_SYM)


def _add_rule(libc: ctypes.CDLL, ruleset_fd: int, path: str, access: int) -> None:
    """Add one PATH_BENEATH rule. Directory-only access rights (create/remove-inside, list) are
    dropped when *path* resolves to a non-directory — the kernel rejects them (EINVAL) for a file
    rule, since those actions only make sense on directory contents, not the leaf file itself."""
    is_dir = True
    try:
        path_fd = os.open(path, os.O_PATH | os.O_DIRECTORY)
    except NotADirectoryError:
        is_dir = False
        try:
            path_fd = os.open(path, os.O_PATH)
        except OSError as exc:
            logger.debug("Landlock: skipping missing path %r: %s", path, exc)
            return
    except OSError as exc:
        # A listed system path missing on this image (e.g. no /sbin) is not fatal — skip it
        # rather than aborting the whole sandbox setup over an optional path.
        logger.debug("Landlock: skipping missing path %r: %s", path, exc)
        return
    effective_access = access if is_dir else (access & ~_ACCESS_FS_DIRECTORY_ONLY)
    try:
        rule = _PathBeneathAttr(allowed_access=effective_access, parent_fd=path_fd)
        ret = libc.syscall(_SYS_landlock_add_rule, ruleset_fd, _LANDLOCK_RULE_PATH_BENEATH,
                           ctypes.byref(rule), 0)
        if ret != 0:
            raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {path!r}")
    finally:
        os.close(path_fd)


def restrict_to_profile_home(
    profile_home: Path, *, extra_read_write_paths: Sequence[str] = (),
    extra_read_only_paths: Sequence[str] = ()) -> None:
    """Irreversibly confine the CALLING process (and every descendant) to *profile_home* plus a
    fixed minimal set of read-only system paths.

    Intended to run as a ``subprocess.Popen(preexec_fn=...)`` callback: after ``fork()``, before
    ``exec()``, in the CHILD only — the parent Hermes process is never restricted. Must be called
    with a path resolved by the CALLER (before fork), never re-derived here from ambient context
    (``HERMES_HOME`` ContextVar overrides do not survive across a bare ``fork()`` reliably for code
    that never touched them in the child).

    Raises on any failure — callers MUST wrap this in a guard that treats a raised exception as
    "abort the child before exec" is NOT what's wanted; see ``restrict_to_profile_home_or_warn``
    for the fail-open wrapper actually wired into ``LocalEnvironment._run_bash``.
    """
    libc = _get_libc()
    if libc is None:
        raise OSError("libc unavailable for Landlock")

    # Own scoped tmp dir instead of the shared system /tmp (see _READ_WRITE_SYSTEM_PATHS'
    # comment for why /tmp itself is never granted). Created here, before restrict_self, since
    # mkdir under profile_home works today (no Landlock rule active yet) but MAKE_DIR would still
    # be covered by the profile_home rule anyway — created eagerly so a command that never mkdirs
    # its own tmp still finds one ready. Env vars set here are inherited by the exec() that
    # follows this preexec_fn, in the same child process.
    tmp_dir = Path(profile_home) / "cache" / "terminal"
    with suppress(OSError):
        tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir_str = str(tmp_dir)
    for env_var in ("TMPDIR", "TMP", "TEMP"):
        os.environ[env_var] = tmp_dir_str

    ruleset_fd = _create_ruleset(libc)
    try:
        _add_rule(libc, ruleset_fd, str(profile_home), _ACCESS_FS_READ_WRITE)
        for path in extra_read_write_paths:
            _add_rule(libc, ruleset_fd, path, _ACCESS_FS_READ_WRITE)
        for path in (*_READ_ONLY_SYSTEM_PATHS, *extra_read_only_paths):
            _add_rule(libc, ruleset_fd, path, _ACCESS_FS_READ_ONLY)
        for path in _READ_WRITE_SYSTEM_PATHS:
            _add_rule(libc, ruleset_fd, path, _ACCESS_FS_READ_WRITE)
        # PR_SET_NO_NEW_PRIVS is a hard prerequisite for an unprivileged landlock_restrict_self.
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")
        ret = libc.syscall(_SYS_landlock_restrict_self, ruleset_fd, 0)
        if ret != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
    finally:
        os.close(ruleset_fd)


def restrict_to_profile_home_or_warn(profile_home: Path) -> None:
    """Fail-open wrapper for ``preexec_fn``: best-effort Landlock confinement, never blocks the
    command. A ``preexec_fn`` exception would abort ``Popen`` entirely (the child never execs),
    which is worse than running one command without the extra sandbox layer — the existing
    AgentPermissions checks are still the primary control."""
    if not landlock_supported():
        return
    try:
        restrict_to_profile_home(profile_home)
    except Exception:  # noqa: BLE001 — must never abort the child's exec
        # No logger call here: this runs post-fork, in the child, with stdio already redirected
        # into the parent's pipes — writing here could interleave with the command's own output.
        # Best-effort only; the AgentPermissions checks remain the primary control either way.
        pass
