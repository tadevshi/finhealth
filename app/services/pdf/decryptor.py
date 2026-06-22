"""Decrypt password-protected PDF statements with :mod:`pikepdf`.

Chilean bank statements are AES-encrypted with a user password
that is a deterministic function of the cardholder's RUT (see
:mod:`app.services.pdf.password_deriver`). The encrypted file is
what the user uploads; downstream stages of the pipeline expect a
plain (unencrypted) PDF so :mod:`pdfplumber` can stream its
content.

:mod:`pikepdf` is preferred over alternatives for three reasons:

* It honours the PDF 2.0 standard encryption profile (AES-256 with
  SHA-256/384 KDF), which the Chilean banks have been rolling out
  since 2024. :mod:`PyPDF2` rejects some of those files with
  "algorithm AES-256-R5 not supported".
* It is a thin C++ wrapper over QPDF — fast, and with no Python
  object graph to serialise, so memory pressure stays low even for
  100-page statements.
* Its error type (``pikepdf.PasswordError``) is specific, which
  lets the orchestrator distinguish "wrong password" from
  "corrupted file" without inspecting message strings.

The function returns the path of the decrypted file rather than
the bytes. The reason is operational: the ingestion pipeline runs
in an async context where streaming a 5 MB blob through the event
loop is wasteful when the next stage can simply re-open the file.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf


class PDFDecryptError(Exception):
    """Raised when a PDF cannot be decrypted.

    The base class covers all decrypt-time failures: wrong
    password, corrupted file, unsupported encryption algorithm.
    The ``__cause__`` always carries the underlying library
    exception so debugging does not require re-running the
    command.
    """


class PDFPasswordError(PDFDecryptError):
    """Raised when the password does not unlock the PDF.

    Subclass of :class:`PDFDecryptError` so callers can choose
    to treat wrong-password differently from "file is corrupted
    beyond repair" if they need to.
    """


def decrypt_pdf(encrypted_path: Path, password: str, output_path: Path) -> Path:
    """Decrypt ``encrypted_path`` and save the plain PDF to ``output_path``.

    The output directory is created if it does not exist. Any
    pre-existing file at ``output_path`` is overwritten — the
    caller is responsible for using a unique path (typically
    ``tempfile.NamedTemporaryFile`` or a UUID-named file in
    ``shared/``).

    Parameters
    ----------
    encrypted_path:
        Path to the encrypted PDF as uploaded by the user.
    password:
        The password to try, normally the result of
        :func:`app.services.pdf.password_deriver.derive_password`.
    output_path:
        Destination path for the decrypted PDF. Created if missing.

    Returns
    -------
    Path
        The same ``output_path``, returned for fluent chaining:
        ``out = decrypt_pdf(src, pwd, tmp)``.

    Raises
    ------
    FileNotFoundError
        If ``encrypted_path`` does not exist. (Re-raised from the
        underlying ``open`` call rather than wrapped, because the
        exception is already self-describing.)
    PDFPasswordError
        If the password is wrong.
    PDFDecryptError
        For any other decryption failure (corrupted file,
        unsupported encryption profile, I/O error during save).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with pikepdf.open(encrypted_path, password=password) as pdf:
            pdf.save(output_path)
    except FileNotFoundError:
        # Re-raise so the caller can distinguish a missing file
        # from a wrong password or a corrupted file.
        raise
    except pikepdf.PasswordError as exc:
        raise PDFPasswordError(
            "PDF password is incorrect or the file is not password-protected"
        ) from exc
    except pikepdf.PdfError as exc:
        # Catches ``pikepdf._core.PdfError`` and its subclasses
        # other than ``PasswordError`` — typically "corrupted
        # file" or "unsupported encryption revision".
        raise PDFDecryptError(f"Failed to decrypt PDF {encrypted_path}: {exc}") from exc
    except OSError as exc:
        # I/O errors during save (no space, permission denied, etc.)
        # are reported as ``PDFDecryptError`` so the orchestrator
        # only has to catch one type, but the message preserves the
        # underlying reason.
        raise PDFDecryptError(f"Failed to write decrypted PDF to {output_path}: {exc}") from exc
    return output_path
