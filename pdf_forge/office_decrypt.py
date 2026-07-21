"""Decrypt a password-protected Office document locally, before conversion.

Both conversion backends need this, for different reasons:

* **Microsoft Office** raises a modal password dialog that no ``DisplayAlerts``
  or ``Interactive`` setting suppresses. A headless run then blocks forever
  (measured - the process had to be killed).
* **LibreOffice** cannot open a document encrypted by Microsoft Office at all.
  The UNO bridge is lost rather than the file converted, and an untrimmed
  install fails identically, so it is a LibreOffice limitation rather than
  anything about how the runtime is provisioned.

Decrypting here removes both problems: the backend only ever sees a plain
document, and a wrong password becomes an ordinary error the caller re-prompts
for instead of a hang or a lost bridge.

Trade-off, deliberately accepted: a decrypted copy exists on disk for the
duration of that one conversion. It is written inside a caller-owned temporary
directory and removed in a ``finally``. The password itself is never written to
disk, never placed on a command line, and never logged.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .constants import LOG_PREFIX

logger = logging.getLogger(LOG_PREFIX)

__all__ = ['DecryptError', 'DecryptPasswordError', 'decrypt_to_temp']


class DecryptError(RuntimeError):
    """The file could not be decrypted locally."""


class DecryptPasswordError(DecryptError):
    """The supplied password does not open this file."""


def decrypt_to_temp(path: Path, password: Optional[str], temp_dir: Path) -> Path:
    """Write a decrypted copy of ``path`` into ``temp_dir`` and return it.

    Raises :class:`DecryptPasswordError` when the password is wrong or missing,
    and :class:`DecryptError` when the container cannot be handled at all - the
    caller must not fall back to handing the encrypted file to a backend, since
    that is exactly the case that hangs or loses the bridge.
    """
    try:
        import msoffcrypto
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise DecryptError(
            "msoffcrypto-tool is not installed, so an encrypted document "
            "cannot be opened."
        ) from exc

    path = Path(path)
    target = Path(temp_dir) / f"decrypted{path.suffix}"
    try:
        with path.open("rb") as handle:
            office_file = msoffcrypto.OfficeFile(handle)
            office_file.load_key(password=password or "", verify_password=True)
            with target.open("wb") as out_handle:
                office_file.decrypt(out_handle)
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot open"
        try:
            target.unlink()
        except OSError:
            pass
        name = type(exc).__name__
        # Classify by type, not by substring. msoffcrypto raises InvalidKeyError
        # for a genuinely wrong password, but its *base* DecryptionError also
        # covers "unsupported EncryptionInfo version" - a file this build simply
        # cannot open with any password. The old `"Decryption" in name` test
        # caught both, so an unsupported-encryption file was reported as "wrong
        # password" and the caller re-prompted forever, never reaching the
        # honest DecryptError branch. Only a true bad-key (or an empty/missing
        # password) is a password error.
        is_bad_key = "InvalidKey" in name or "Password" in name or not password
        if is_bad_key:
            raise DecryptPasswordError("wrong password") from None
        raise DecryptError(
            f"This encrypted file could not be decrypted locally ({name}: {exc})."
        ) from None
    return target
