"""Shared diagnostics for repository-local artifact writes."""

from __future__ import annotations

import getpass
import grp
import json
import os
import pwd
import shlex
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ArtifactWriteError(PermissionError):
    """Raised when an artifact path cannot be written safely."""

    def __init__(self, *, path: Path, generator: str, reason: str, detail: Mapping[str, Any] | None = None) -> None:
        self.path = Path(path)
        self.generator = str(generator)
        self.reason = str(reason)
        self.detail = dict(detail or {})
        super().__init__(f"{self.generator}: {self.reason}: {self.path}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "error_type": "artifact_write_permission_error",
            "generator": self.generator,
            "path": str(self.path),
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RuntimeIdentity:
    user: str
    group: str
    uid: int
    gid: int
    groups: tuple[int, ...]


def runtime_identity(user: str | None = None, group: str | None = None) -> RuntimeIdentity:
    user_s = str(user or os.environ.get("ALGO_RUNTIME_USER") or getpass.getuser()).strip() or getpass.getuser()
    try:
        pw = pwd.getpwnam(user_s)
        uid = int(pw.pw_uid)
        default_gid = int(pw.pw_gid)
    except KeyError:
        uid = os.getuid()
        default_gid = os.getgid()
        user_s = getpass.getuser()
    group_s = str(group or os.environ.get("ALGO_RUNTIME_GROUP") or "").strip()
    if group_s:
        try:
            gid = int(grp.getgrnam(group_s).gr_gid)
        except KeyError:
            gid = default_gid
            group_s = grp.getgrgid(gid).gr_name
    else:
        gid = default_gid
        try:
            group_s = grp.getgrgid(gid).gr_name
        except KeyError:
            group_s = str(gid)
    groups = {gid}
    try:
        groups.update(g.gr_gid for g in grp.getgrall() if user_s in g.gr_mem)
    except Exception:
        pass
    return RuntimeIdentity(user=user_s, group=group_s, uid=uid, gid=gid, groups=tuple(sorted(groups)))


def _owner_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def _mode_text(mode: int) -> str:
    return oct(stat.S_IMODE(mode))[2:].rjust(3, "0")


def _writable_by_identity(st: os.stat_result, identity: RuntimeIdentity) -> bool:
    mode = stat.S_IMODE(st.st_mode)
    if st.st_uid == identity.uid:
        return bool(mode & stat.S_IWUSR)
    if st.st_gid in identity.groups:
        return bool(mode & stat.S_IWGRP)
    return bool(mode & stat.S_IWOTH)


def artifact_target_diagnostics(path: Path | str, *, runtime_user: str | None = None, runtime_group: str | None = None) -> dict[str, Any]:
    target = Path(path)
    parent = target if target.suffix == "" else target.parent
    ident = runtime_identity(runtime_user, runtime_group)
    out: dict[str, Any] = {
        "path": str(target),
        "directory": str(parent),
        "runtime_user": ident.user,
        "runtime_group": ident.group,
        "runtime_uid": ident.uid,
        "runtime_gid": ident.gid,
        "exists": parent.exists(),
        "can_create_directory": False,
        "target_user_writable": False,
        "current_process_writable": False,
        "world_writable": False,
        "error": None,
    }
    try:
        if parent.exists():
            st = parent.stat()
            out.update(
                {
                    "owner": _owner_name(st.st_uid),
                    "group": _group_name(st.st_gid),
                    "mode": _mode_text(st.st_mode),
                    "target_user_writable": _writable_by_identity(st, ident),
                    "current_process_writable": os.access(parent, os.W_OK | os.X_OK),
                    "world_writable": bool(stat.S_IMODE(st.st_mode) & stat.S_IWOTH),
                }
            )
        else:
            existing = parent
            while not existing.exists() and existing.parent != existing:
                existing = existing.parent
            st = existing.stat()
            out.update(
                {
                    "nearest_existing_parent": str(existing),
                    "nearest_parent_owner": _owner_name(st.st_uid),
                    "nearest_parent_group": _group_name(st.st_gid),
                    "nearest_parent_mode": _mode_text(st.st_mode),
                    "can_create_directory": _writable_by_identity(st, ident),
                }
            )
    except OSError as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def artifact_file_readable_by_runtime(path: Path | str, *, runtime_user: str | None = None, runtime_group: str | None = None) -> bool:
    target = Path(path)
    if not target.exists():
        return False
    ident = runtime_identity(runtime_user, runtime_group)
    st = target.stat()
    mode = stat.S_IMODE(st.st_mode)
    if st.st_uid == ident.uid:
        return bool(mode & stat.S_IRUSR)
    if st.st_gid in ident.groups:
        return bool(mode & stat.S_IRGRP)
    return bool(mode & stat.S_IROTH)


def ensure_artifact_directory(path: Path | str, *, generator: str, runtime_user: str | None = None, runtime_group: str | None = None) -> Path:
    directory = Path(path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArtifactWriteError(
            path=directory,
            generator=generator,
            reason="mkdir_failed",
            detail={"exception": f"{type(exc).__name__}: {exc}"},
        ) from exc
    diag = artifact_target_diagnostics(directory, runtime_user=runtime_user, runtime_group=runtime_group)
    if not diag.get("target_user_writable"):
        raise ArtifactWriteError(path=directory, generator=generator, reason="directory_not_writable_by_runtime_user", detail=diag)
    return directory


def _assert_process_matches_runtime(target: Path, *, generator: str) -> None:
    ident = runtime_identity()
    if os.geteuid() == ident.uid:
        return
    raise ArtifactWriteError(
        path=target,
        generator=generator,
        reason="process_user_mismatch",
        detail={
            **artifact_target_diagnostics(target),
            "process_uid": os.geteuid(),
            "process_user": _owner_name(os.geteuid()),
            "expected_runtime_uid": ident.uid,
            "expected_runtime_user": ident.user,
        },
    )


def atomic_write_text(path: Path | str, text: str, *, generator: str) -> None:
    target = Path(path)
    ensure_artifact_directory(target.parent, generator=generator)
    _assert_process_matches_runtime(target, generator=generator)
    fd: int | None = None
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.replace(target)
        target.chmod(0o644)
    except OSError as exc:
        raise ArtifactWriteError(
            path=target,
            generator=generator,
            reason="atomic_write_failed",
            detail={**artifact_target_diagnostics(target), "exception": f"{type(exc).__name__}: {exc}"},
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_path is not None:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass


def repair_command_for_project_artifacts(root: Path | str, *, runtime_user: str = "algosphere", runtime_group: str = "algosphere") -> str:
    """Return a narrow operator command for repairing known artifact directories."""

    base = Path(root).resolve()
    targets = [
        base / "data" / "research_metrics",
        base / "reports",
        base / "data" / "logs",
    ]
    quoted = " ".join(shlex.quote(str(path)) for path in targets)
    return (
        f"sudo chown -R {runtime_user}:{runtime_group} {quoted} && "
        f"find {quoted} -type d -exec chmod u+rwx,go+rx {{}} + && "
        f"find {quoted} -type f -exec chmod u+rw,go+r {{}} +"
    )


def check_atomic_writability(
    directory: Path | str,
    *,
    filename: str = ".artifact_writability_check.tmp",
    generator: str = "artifact_writability_check",
    runtime_user: str | None = None,
    runtime_group: str | None = None,
) -> dict[str, Any]:
    target_dir = ensure_artifact_directory(directory, generator=generator, runtime_user=runtime_user, runtime_group=runtime_group)
    target = target_dir / filename
    renamed = target_dir / f"{filename}.renamed"
    result = artifact_target_diagnostics(target, runtime_user=runtime_user, runtime_group=runtime_group)
    result.update({"atomic_create": False, "atomic_rename": False, "cleanup": False})
    fd: int | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".tmp", dir=str(target_dir))
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            fh.write(json.dumps({"ok": True}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(target)
        result["atomic_create"] = target.exists()
        target.replace(renamed)
        result["atomic_rename"] = renamed.exists()
        renamed.unlink()
        result["cleanup"] = not renamed.exists() and not target.exists()
        result["ok"] = bool(result["atomic_create"] and result["atomic_rename"] and result["cleanup"])
    except OSError as exc:
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise ArtifactWriteError(path=target, generator=generator, reason="atomic_writability_check_failed", detail=result) from exc
    finally:
        if fd is not None:
            os.close(fd)
        for candidate in (target, renamed):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
    return result
