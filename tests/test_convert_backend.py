"""Backend selection, Microsoft Office automation, and runtime trimming.

Covers the two-step choice the user sees when converting a document:
Microsoft Office if it is installed, otherwise an opt-in LibreOffice install.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from pdf_forge import convert_backend as cb
from pdf_forge import msoffice
from pdf_forge import office_decrypt
from pdf_forge import office_provision as ort_provision
from pdf_forge import office_runtime as ort
from pdf_forge import ops_office

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows/COM only")


# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #

def test_office_is_preferred_over_libreoffice(monkeypatch):
    """Installed Office wins: it needs no download and no disk space."""
    monkeypatch.setattr(msoffice, "detect_office",
                        lambda: {"apps": ["word", "excel"], "families": ["word"]})
    monkeypatch.setattr(ort, "runtime_status", lambda *a, **k: {"ready": True})
    choice = cb.detect_backend()
    assert choice.kind == cb.MSOFFICE
    assert "Word" in cb.backend_label(choice)


def test_libreoffice_is_used_when_office_is_absent(monkeypatch):
    monkeypatch.setattr(msoffice, "detect_office", lambda: None)
    monkeypatch.setattr(ort, "runtime_status",
                        lambda *a, **k: {"ready": True, "libreoffice_version": "25.8"})
    choice = cb.detect_backend()
    assert choice.kind == cb.LIBREOFFICE
    assert bool(choice) is True


def test_no_backend_is_falsy(monkeypatch):
    monkeypatch.setattr(msoffice, "detect_office", lambda: None)
    monkeypatch.setattr(ort, "runtime_status", lambda *a, **k: {"ready": False})
    assert not cb.detect_backend()


def test_nothing_is_installed_without_being_asked(monkeypatch, capsys):
    """LibreOffice must never be provisioned as a silent startup prerequisite."""
    monkeypatch.setattr(msoffice, "detect_office", lambda: None)
    monkeypatch.setattr(ort, "runtime_status", lambda *a, **k: {"ready": False})
    monkeypatch.setattr(ort, "provision_runtime", _must_not_run)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert not ops_office._resolve_backend()


def test_the_install_prompt_defaults_to_yes(monkeypatch):
    """A blank Enter accepts the install, as the [y] marker promises."""
    monkeypatch.setattr(msoffice, "detect_office", lambda: None)
    monkeypatch.setattr(ort, "runtime_status", lambda *a, **k: {"ready": False})
    called = {"n": 0}

    def fake_provision(**_kwargs):
        called["n"] += 1
        return {"status": "installed"}

    monkeypatch.setattr(ort, "provision_runtime", fake_provision)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    ops_office._resolve_backend()
    assert called["n"] == 1, "an empty answer must accept the default"


def _must_not_run(*_a, **_k):  # pragma: no cover - only runs on failure
    raise AssertionError("provisioning ran without the user agreeing")


# --------------------------------------------------------------------------- #
# Microsoft Office detection
# --------------------------------------------------------------------------- #

@windows_only
def test_detection_matches_the_registry():
    """Detection is decided by COM registration, not by files on disk."""
    import winreg

    expected = []
    for family, progid in msoffice._PROGIDS.items():
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\CLSID"):
                expected.append(family)
        except OSError:
            pass
    detected = msoffice.detect_office()
    assert (detected["apps"] if detected else []) == expected


def test_detection_is_none_without_com(monkeypatch):
    monkeypatch.setattr(msoffice.os, "name", "posix")
    assert msoffice.detect_office() is None
    assert msoffice.is_available() is False
    assert msoffice.describe_office(None) == "not installed"


def test_csv_is_routed_to_excel():
    assert msoffice._FAMILY_APP["csv"] == "excel"
    assert "csv" in msoffice.office_families(["excel"])
    assert "csv" not in msoffice.office_families(["word"])


# --------------------------------------------------------------------------- #
# Passwords: a wrong one must never reach Office (it would hang on a dialog)
# --------------------------------------------------------------------------- #

def test_a_wrong_password_is_reported_not_passed_to_office(tmp_path, monkeypatch):
    class Boom(Exception):
        pass

    Boom.__name__ = "InvalidKeyError"

    class FakeOfficeFile:
        def __init__(self, _handle):
            pass

        def load_key(self, **_kwargs):
            raise Boom()

    monkeypatch.setitem(__import__("sys").modules, "msoffcrypto",
                        mock.Mock(OfficeFile=FakeOfficeFile))
    src = tmp_path / "locked.xlsx"
    src.write_bytes(b"not really an office file")
    with pytest.raises(office_decrypt.DecryptPasswordError):
        msoffice.decrypt_to_temp(src, "wrong", tmp_path)


def test_an_unverifiable_container_is_refused(tmp_path, monkeypatch):
    """If the file cannot be decrypted locally it must not reach Office."""
    class FakeOfficeFile:
        def __init__(self, _handle):
            raise RuntimeError("unsupported container")

    monkeypatch.setitem(__import__("sys").modules, "msoffcrypto",
                        mock.Mock(OfficeFile=FakeOfficeFile))
    src = tmp_path / "weird.docx"
    src.write_bytes(b"x")
    with pytest.raises(office_decrypt.DecryptError) as excinfo:
        msoffice.decrypt_to_temp(src, "pw", tmp_path)
    assert not isinstance(excinfo.value, office_decrypt.DecryptPasswordError)


def test_a_failed_decrypt_leaves_no_plaintext_behind(tmp_path, monkeypatch):
    class Boom(Exception):
        pass

    Boom.__name__ = "DecryptionError"

    class FakeOfficeFile:
        def __init__(self, _handle):
            pass

        def load_key(self, **_kwargs):
            raise Boom()

    monkeypatch.setitem(__import__("sys").modules, "msoffcrypto",
                        mock.Mock(OfficeFile=FakeOfficeFile))
    src = tmp_path / "locked.docx"
    src.write_bytes(b"x")
    with pytest.raises(office_decrypt.DecryptPasswordError):
        msoffice.decrypt_to_temp(src, None, tmp_path)
    assert list(tmp_path.glob("decrypted*")) == []


def test_the_session_never_logs_a_password(caplog, monkeypatch):
    """A password must not reach a log record through any code path."""
    session = msoffice.MsOfficeSession()

    def explode(*_a, **_k):
        raise msoffice.MsOfficeError("Word could not be started")

    monkeypatch.setattr(session, "_app", explode)
    with caplog.at_level("DEBUG"):
        with pytest.raises(msoffice.MsOfficeError):
            session.convert(Path("x.docx"), Path("o.pdf"), "word",
                            password="hunter2", encrypted=False)
    assert "hunter2" not in caplog.text


# --------------------------------------------------------------------------- #
# Runtime trimming
# --------------------------------------------------------------------------- #

def test_trimming_keeps_what_conversion_actually_needs():
    """The engine, its fonts and its bundled Python are never trimmed.

    Removing any of these was measured to break conversion, so a future edit
    that adds them to the trim list must fail here rather than in the field.
    """
    essential = ("share/registry", "share/config", "Fonts", "program/python-core")
    for path in essential:
        assert not any(entry.startswith(path) for entry in ort_provision._TRIMMABLE), path


def test_trimming_removes_the_listed_components(tmp_path):
    for relative in ("share/extensions", "program/resource", "help"):
        target = tmp_path / relative
        target.mkdir(parents=True)
        (target / "payload.bin").write_bytes(b"x" * 4096)
    keep = tmp_path / "share" / "registry"
    keep.mkdir(parents=True)
    (keep / "keep.bin").write_bytes(b"y" * 1024)

    freed = ort_provision._trim_runtime(tmp_path)

    assert freed >= 3 * 4096
    assert not (tmp_path / "share" / "extensions").exists()
    assert not (tmp_path / "program" / "resource").exists()
    assert (keep / "keep.bin").exists(), "essential components must survive"


def test_trimming_a_missing_component_is_not_an_error(tmp_path):
    assert ort_provision._trim_runtime(tmp_path) == 0


# --------------------------------------------------------------------------- #
# CSV encoding: Excel guesses the ANSI code page without a BOM
# --------------------------------------------------------------------------- #

def test_a_utf8_csv_gets_a_bom_for_excel(tmp_path):
    """Regression: Persian CSV text imported as mojibake ("سلام" -> "Ø³Ù„Ø§Ù…")."""
    src = tmp_path / "data.csv"
    src.write_bytes("a,b\n1,سلام\n".encode("utf-8"))
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    handed_over = msoffice._csv_with_bom(src, scratch)

    assert handed_over != src, "the source must not be handed over unchanged"
    assert handed_over.read_bytes().startswith(b"\xef\xbb\xbf")
    assert handed_over.read_bytes()[3:] == src.read_bytes(), "content must survive"
    assert src.read_bytes() == "a,b\n1,سلام\n".encode("utf-8"), "source untouched"


def test_a_csv_that_already_has_a_bom_is_passed_through(tmp_path):
    src = tmp_path / "data.csv"
    src.write_bytes(b"\xef\xbb\xbf" + "x,س\n".encode("utf-8"))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert msoffice._csv_with_bom(src, scratch) == src
    assert list(scratch.iterdir()) == []


def test_a_non_utf8_csv_is_left_alone(tmp_path):
    """Guessing an encoding here would corrupt what the CSV pipeline resolved."""
    src = tmp_path / "legacy.csv"
    src.write_bytes(b"a,b\n1,\xe4\xf6\xfc\n")   # cp1252, invalid UTF-8
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert msoffice._csv_with_bom(src, scratch) == src


# --------------------------------------------------------------------------- #
# Both backends decrypt locally: neither can open an encrypted source directly
# --------------------------------------------------------------------------- #

def test_both_backends_share_one_decryptor():
    """LibreOffice loses its bridge on an encrypted source; Office hangs on a
    dialog. Neither may ever be handed the encrypted file itself."""
    from pdf_forge import office_decrypt, ops_office

    assert msoffice.decrypt_to_temp is office_decrypt.decrypt_to_temp
    assert ops_office.decrypt_to_temp is office_decrypt.decrypt_to_temp


def test_libreoffice_path_never_forwards_a_password(tmp_path, monkeypatch):
    """The password goes to the local decryptor, not to the conversion server."""
    from pdf_forge import office_decrypt, ops_office

    seen = {}

    def fake_decrypt(path, password, temp_dir):
        seen["password"] = password
        plain = Path(temp_dir) / "decrypted.xlsx"
        plain.write_bytes(b"plain")
        return plain

    def fake_convert(_server, src, out, **kwargs):
        seen["convert_kwargs"] = kwargs
        seen["converted"] = Path(src).name
        Path(out).write_bytes(b"%PDF-1.7\n")

    monkeypatch.setattr(ops_office, "decrypt_to_temp", fake_decrypt)
    monkeypatch.setattr(ops_office.ort, "convert_to_pdf", fake_convert)
    monkeypatch.setattr(ops_office, "_validate_pdf_output", lambda _p: None)
    monkeypatch.setattr(ops_office, "_prompt_output_protection", lambda _n: None)
    monkeypatch.setattr(ops_office, "is_encrypted_office_file", lambda _p: True)
    monkeypatch.setattr(ops_office, "_prompt_convert_password", lambda *_a: "s3cret")

    src = tmp_path / "locked.xlsx"
    src.write_bytes(b"encrypted")
    job = {"src": src, "family": "excel", "out": tmp_path / "locked.pdf",
           "csv_dialect": None}
    assert ops_office._convert_one(object(), job, cb.BackendChoice(cb.LIBREOFFICE)) == "ok"

    assert seen["password"] == "s3cret", "the decryptor must receive the password"
    assert seen["converted"] == "decrypted.xlsx", "the server must get the plain copy"
    assert not seen["convert_kwargs"].get("password"), (
        "the password must not be forwarded to the conversion server"
    )
    assert office_decrypt.decrypt_to_temp is not fake_decrypt  # sanity: patch scoped


def test_a_decrypt_failure_reprompts_instead_of_failing(tmp_path, monkeypatch):
    from pdf_forge import office_decrypt, ops_office

    attempts = iter(["wrong", "right"])

    def fake_decrypt(path, password, temp_dir):
        if password != "right":
            raise office_decrypt.DecryptPasswordError("wrong password")
        plain = Path(temp_dir) / "decrypted.xlsx"
        plain.write_bytes(b"plain")
        return plain

    monkeypatch.setattr(ops_office, "decrypt_to_temp", fake_decrypt)
    monkeypatch.setattr(ops_office.ort, "convert_to_pdf",
                        lambda _s, _i, out, **_k: Path(out).write_bytes(b"%PDF-1.7\n"))
    monkeypatch.setattr(ops_office, "_validate_pdf_output", lambda _p: None)
    monkeypatch.setattr(ops_office, "_prompt_output_protection", lambda _n: None)
    monkeypatch.setattr(ops_office, "is_encrypted_office_file", lambda _p: True)
    monkeypatch.setattr(ops_office, "_prompt_convert_password",
                        lambda *_a: next(attempts))

    src = tmp_path / "locked.xlsx"
    src.write_bytes(b"encrypted")
    job = {"src": src, "family": "excel", "out": tmp_path / "out.pdf",
           "csv_dialect": None}
    assert ops_office._convert_one(object(), job, cb.BackendChoice(cb.LIBREOFFICE)) == "ok"
    assert next(attempts, "used") == "used", "a wrong password must re-prompt"
