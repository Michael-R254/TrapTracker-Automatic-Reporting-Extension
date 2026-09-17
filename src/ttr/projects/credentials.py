"""Mailbox credential storage and resolution.

**The OS keyring is the storage mechanism. An environment variable is a
documented escape hatch. There is no file-based fallback, ever.**

The upstream system's own README records that it stores email app passwords in
plaintext in SQLite. Not reproducing that is
a deliberate improvement, and a "just this once" plaintext fallback would undo it
- so the failure path here reports that no backend is available and stops, rather
than quietly writing the secret somewhere it can be read.

On Windows the backend is ``WinVaultKeyring`` (Windows Credential Manager), which
needs no D-Bus and no desktop session. On a headless Linux host there may be no
backend at all; that is a reportable state, answered by the env-var escape hatch,
not routed around with a local file.

The env var is not a plaintext file in disguise: nothing here writes it, it never
lands in the project directory, and it cannot be swept into a commit by
``git add -A``. It is the ordinary way a secret reaches a headless service.

Nothing in this module logs, returns or formats a password. Resolution logs its
SOURCE only - a stale exported variable silently overriding a correct keyring
entry is otherwise very hard to spot.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

from pydantic import SecretStr

from ..logging import get_logger
from .errors import ProjectError

logger = get_logger(__name__)

#: Keyring service name. The account is the project id, so one machine can hold
#: credentials for any number of projects without collision.
SERVICE = "traptracker-report"

#: Env var scoped to one project. Always valid.
ENV_PREFIX = "TTR_IMAP_PASSWORD__"
#: Unscoped env var. Valid ONLY when a single project is in scope.
ENV_UNSCOPED = "TTR_IMAP_PASSWORD"

#: Every run of whitespace is removed, not just the ends. Gmail displays an app
#: password as four space-separated groups ("abcd efgh ijkl mnop") and people
#: paste it exactly as shown; the actual secret is the 16 characters. A password
#: cannot legitimately contain whitespace here, so this is lossless.
_WHITESPACE = re.compile(r"\s+")


class CredentialError(ProjectError):
    """Base for credential failures. Subclasses are distinguishable on purpose."""


class NoCredentialBackend(CredentialError):
    """No OS credential store on this machine."""


class NoCredentialStored(CredentialError):
    """A backend exists, but holds no entry for this project."""


class AmbiguousCredentialScope(CredentialError):
    """The unscoped env var was set with more than one project in scope."""


class CredentialScopeNotStated(CredentialError):
    """A caller resolved a credential without saying how many projects are in scope.

    A wiring bug, not a configuration problem. It exists because the count used
    to default to 1, and every ingest path took that default. So the refusal
    above never fired, and one exported password was tried against whichever
    project was ingesting.
    """


def env_var_for(project_id: str) -> str:
    """The project-scoped env var name. Uppercased, non-alphanumerics to ``_``."""
    return ENV_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", project_id).upper()


def credential_ref_for(project_id: str) -> str:
    """The manifest's reference. A locator, never a secret."""
    return f"{SERVICE}:{project_id}"


def normalise(password: str) -> str:
    """Strip every whitespace run - see ``_WHITESPACE``."""
    return _WHITESPACE.sub("", password or "")


@dataclass(frozen=True)
class ResolvedCredential:
    """A password plus WHERE it came from. The value never leaves as a str."""

    secret: SecretStr
    source: str                      # human-readable provenance, no value in it

    def __repr__(self) -> str:       # belt and braces over SecretStr's own repr
        return f"ResolvedCredential(source={self.source!r})"


# --------------------------------------------------------------------- backend
def _keyring():
    """The keyring module, imported lazily so the rest of the CLI does not pay
    for it and so a broken install surfaces here with a usable message."""
    try:
        import keyring
    except ImportError as exc:       # pragma: no cover - packaging failure
        raise NoCredentialBackend(
            "the 'keyring' package is not installed - reinstall this project's "
            "dependencies (pip install -e .)") from exc
    return keyring


def backend_name() -> str:
    """A short description of the active backend, for diagnostics."""
    try:
        backend = _keyring().get_keyring()
    except CredentialError:
        raise
    except Exception as exc:
        return f"(unavailable: {exc!r})"
    name = type(backend).__name__
    if type(backend).__module__ == "keyring.backends.fail":
        # keyring's NULL backend is also called `Keyring`, so the bare class name
        # reads as a working store on a machine that has none - measured in a
        # container, where this printed "Keyring" and meant the opposite.
        return f"{name} (keyring.backends.fail - no store)"
    return name


def backend_available() -> bool:
    """Whether a usable credential store exists on this machine."""
    try:
        kr = _keyring().get_keyring()
    except CredentialError:
        return False
    except Exception:
        return False
    # keyring's null backend is what you get when nothing real is installed.
    return type(kr).__module__ != "keyring.backends.fail"


_NO_BACKEND_MESSAGE = (
    "No OS credential store is available on this machine.\n"
    "\n"
    "This project stores mailbox passwords in the operating system's credential\n"
    "manager and deliberately has no file-based fallback - writing the secret to\n"
    "disk is the thing being avoided.\n"
    "\n"
    "On a headless host, supply the password through the environment instead:\n"
    "    {env_var}=<app password>\n"
    "That is read directly by this process; it is never written to the project\n"
    "directory and cannot be committed."
)


def _require_backend(project_id: str) -> None:
    if not backend_available():
        raise NoCredentialBackend(
            _NO_BACKEND_MESSAGE.format(env_var=env_var_for(project_id)))


# --------------------------------------------------------------------- storage
def store(project_id: str, password: str) -> str:
    """Put a password in the OS credential store. Returns the manifest ref."""
    _require_backend(project_id)
    value = normalise(password)
    if not value:
        raise CredentialError("an empty password cannot be stored")
    kr = _keyring()
    try:
        kr.set_password(SERVICE, project_id, value)
    except Exception as exc:
        raise CredentialError(
            f"the credential store refused to save the password: {exc}") from exc
    logger.info("credential_stored",
                extra={"project": project_id, "backend": backend_name()})
    return credential_ref_for(project_id)


def delete(project_id: str) -> bool:
    """Remove a stored password. Returns whether one was actually there.

    An absent entry is NOT an error: deleting a project that never had a
    credential stored is an ordinary case, not a failure to report.
    """
    kr = _keyring()
    try:
        existing = kr.get_password(SERVICE, project_id)
    except Exception:
        existing = None
    if existing is None:
        return False
    try:
        kr.delete_password(SERVICE, project_id)
    except Exception as exc:
        raise CredentialError(
            f"the stored mailbox credential could not be removed: {exc}") from exc
    logger.info("credential_deleted", extra={"project": project_id})
    return True


def supply_command(project_id: str) -> str:
    """What gives this project a password, on THIS machine.

    `ttr project set-password` writes to the OS credential store. On a machine
    without one, such as a container or a headless host, naming it sends the
    user to a command that cannot succeed. There the per-project variable is
    the route.
    """
    if backend_available():
        return f"ttr project set-password {project_id[:8]}"
    return f"{env_var_for(project_id)}=<app password>"


def supply_step(project_id: str) -> str:
    """`supply_command` as the next step in a sentence."""
    if backend_available():
        return f"run `{supply_command(project_id)}`"
    return (f"set `{supply_command(project_id)}` in the environment (this machine "
            f"has no OS credential store)")


def has_stored(project_id: str) -> bool:
    if not backend_available():
        return False
    try:
        return _keyring().get_password(SERVICE, project_id) is not None
    except Exception:
        return False


# ------------------------------------------------------------------ resolution
_NO_ENTRY_MESSAGE = (
    "No mailbox credential is stored for this project.\n"
    "\n"
    "A credential store IS available ({backend}); it simply holds no entry for\n"
    "{project_id}. That is a different problem from a missing backend, and the\n"
    "fix is:\n"
    "    ttr project set-password {project_id}\n"
    "\n"
    "Or supply it through the environment for this run only:\n"
    "    {env_var}=<app password>"
)

_AMBIGUOUS_SCOPE_MESSAGE = (
    "{unscoped} is set, but {count} projects are in scope for this command.\n"
    "\n"
    "An unscoped password cannot be applied to more than one project - doing so\n"
    "would silently try one mailbox's credential against another's. Either:\n"
    "  * name the project on the command (--project <name-or-id>). `ttr serve`\n"
    "    cannot do this: one server reaches every project; or\n"
    "  * use the per-project variable instead, which is always unambiguous:\n"
    "        {example}=<app password>"
)


def resolve(project_id: str, *, projects_in_scope: int = 1) -> ResolvedCredential:
    """Find this project's password. See the module docstring for the order.

    ``projects_in_scope`` is how many projects the current command operates on.
    The unscoped env var is refused when that is not exactly one, rather than
    being applied to whichever project happens to be first.
    """
    scoped_var = env_var_for(project_id)
    scoped = os.environ.get(scoped_var)
    if scoped:
        logger.info("imap_credential_source",
                    extra={"project": project_id, "source": scoped_var})
        return ResolvedCredential(SecretStr(normalise(scoped)),
                                  f"environment ({scoped_var})")

    unscoped = os.environ.get(ENV_UNSCOPED)
    if unscoped:
        if projects_in_scope != 1:
            raise AmbiguousCredentialScope(_AMBIGUOUS_SCOPE_MESSAGE.format(
                unscoped=ENV_UNSCOPED, count=projects_in_scope,
                example=scoped_var))
        logger.info("imap_credential_source",
                    extra={"project": project_id, "source": ENV_UNSCOPED})
        return ResolvedCredential(SecretStr(normalise(unscoped)),
                                  f"environment ({ENV_UNSCOPED})")

    _require_backend(project_id)
    ref = credential_ref_for(project_id)
    try:
        stored = _keyring().get_password(SERVICE, project_id)
    except Exception as exc:
        raise CredentialError(
            f"the credential store could not be read: {exc}") from exc
    if stored is None:
        raise NoCredentialStored(_NO_ENTRY_MESSAGE.format(
            backend=backend_name(), project_id=project_id, env_var=scoped_var))

    logger.info("imap_credential_source",
                extra={"project": project_id, "source": f"keyring ({ref})"})
    return ResolvedCredential(SecretStr(stored), f"keyring ({ref})")


def describe_source(project_id: str, *, projects_in_scope: int = 1) -> Optional[str]:
    """Where a password WOULD come from, without reading its value.

    Used by diagnostics that want to report provenance without resolving. It
    follows `resolve`'s order and its refusal, so a password `resolve` would
    refuse is never reported as available.
    """
    if os.environ.get(env_var_for(project_id)):
        return f"environment ({env_var_for(project_id)})"
    if os.environ.get(ENV_UNSCOPED):
        if projects_in_scope != 1:
            return None              # `resolve` refuses here, before the keyring
        return f"environment ({ENV_UNSCOPED})"
    if has_stored(project_id):
        return f"keyring ({credential_ref_for(project_id)})"
    return None
