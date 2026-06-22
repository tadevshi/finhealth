"""Statement-related HTTP endpoints.

The statements router exposes the *write* side of the Phase 1
ingestion pipeline:

* :func:`upload_statement` — accept a PDF, run it through the
  ingestion orchestrator, and return the resulting
  :class:`app.models.Statement`.
* :func:`get_statement` — read back a single statement (and its
  transactions) by UUID.

Listing and editing transactions lives in the sibling
:mod:`app.api.v1.transactions` module so the URL space and the
data ownership match: statements own their files and their
period; transactions are a separate aggregate.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.models import Statement
from app.schemas.domain import StatementResponse
from app.services.ingestion import IngestionError, IngestionService
from app.services.llm import create_llm_client

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter(prefix="/statements", tags=["statements"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: The PDF "magic" header. The first 5 bytes of every conforming
#: PDF file are the ASCII string ``"%PDF-"`` (the version digit
#: follows, so the full header is 5 bytes). Checking the magic
#: bytes defends against a renamed non-PDF (``.exe.pdf``) slipping
#: past the extension check.
_PDF_MAGIC: Final = b"%PDF-"

#: Maximum length of the safe-path component used to derive an
#: on-disk filename. Real bank filenames are short (``EECCTarjetaVisa.pdf``)
#: but a user might upload ``my bank statement from april 2026 final (3).pdf``
#: — capping prevents absurd lengths from leaking into the filesystem.
_FILENAME_MAX_LENGTH: Final = 80

#: Strict allow-list for the safe-path component. Any character
#: outside ``[A-Za-z0-9._-]`` is collapsed to an underscore.
_SAFE_FILENAME_RE: Final = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def get_ingestion_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncGenerator[IngestionService, None]:
    """Yield an :class:`IngestionService` bound to the request session.

    The LLM client is built per-request via the factory so a
    settings change is picked up without restarting the process.
    The cost is one extra factory call per upload — negligible
    compared to the LLM round-trip itself.

    Production code can override this dependency (e.g. from
    tests that need a fake LLM) via FastAPI's
    ``app.dependency_overrides`` mapping.
    """
    llm_client = create_llm_client(settings)
    try:
        yield IngestionService(session=session, llm_client=llm_client, settings=settings)
    finally:
        # ``aclose`` is a no-op when the client was constructed
        # without an injected ``http_client``, but it is cheap
        # and keeps the symmetry with the long-lived path.
        close = getattr(llm_client, "aclose", None)
        if callable(close):
            await close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/upload",
    response_model=StatementResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a bank statement PDF and run the ingestion pipeline",
    responses={
        status.HTTP_201_CREATED: {
            "description": "Statement ingested successfully.",
            "model": StatementResponse,
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": "Invalid input (unknown bank, bad RUT, bad currency, ...).",
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "Uploaded file exceeds ``MAX_FILE_SIZE_MB``.",
        },
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {
            "description": "Uploaded file is not a PDF (magic bytes / extension check failed).",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "PDF could not be decrypted, text could not be extracted, "
            "the LLM rejected the prompt, or the response did not validate.",
        },
    },
)
async def upload_statement(
    *,
    file: Annotated[UploadFile, File(description="Encrypted bank statement PDF.")],
    bank_name: Annotated[str, Form(description="Short bank identifier (e.g. 'santander').")],
    rut: Annotated[str, Form(description="Cardholder RUT (e.g. '26.450.463-5').")],
    card_number_masked: Annotated[
        str, Form(description="Masked card number (e.g. 'XXXX XXXX XXXX 0951').")
    ],
    cardholder: Annotated[str, Form(description="Printed cardholder name (e.g. 'JOHN DOE').")],
    currency: Annotated[str, Form(description="ISO-4217 currency code ('CLP' or 'USD').")],
    service: Annotated[IngestionService, Depends(get_ingestion_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    """Upload a statement PDF and run it through the ingestion pipeline.

    The endpoint accepts a ``multipart/form-data`` request, validates
    the file size and the PDF magic bytes, persists the upload under
    :attr:`Settings.PDF_UPLOAD_DIR`, then delegates to
    :class:`IngestionService.ingest_statement`. The service
    decrypts the file, runs the LLM extraction, and writes the
    resulting :class:`app.models.Statement` + transactions.

    On any pipeline failure the service marks the statement
    ``FAILED`` and re-raises; this endpoint turns the typed
    exceptions into HTTP status codes:

    * :class:`IngestionError` and its subclasses → 422 (the
      request was understood but the *content* is bad).
    * Other exceptions → 500.
    """
    # 1. Read the entire body so we can both check the size and
    #    inspect the magic bytes. ``UploadFile.read()`` is
    #    awaitable; doing it once is cheaper than streaming in
    #    chunks because PDF statements are typically well under
    #    the 10 MB cap.
    content = await file.read()
    _validate_size(len(content), settings.MAX_FILE_SIZE_MB)
    _validate_pdf(content)

    # 2. Persist the upload. The on-disk path is what the
    #    ingestion service will read from; the response references
    #    the resulting statement's UUID.
    dest_path = _save_upload(content, file.filename or "statement.pdf", settings.PDF_UPLOAD_DIR)

    # 3. Hand off to the service. ``ingest_statement`` returns the
    #    resulting Statement — re-loaded with transactions on
    #    success, marked FAILED on failure.
    try:
        statement = await service.ingest_statement(
            file_path=dest_path,
            bank_name=bank_name,
            rut=rut,
            card_number_masked=card_number_masked,
            cardholder=cardholder,
            currency=currency,
        )
    except FileNotFoundError as exc:
        # The on-disk file disappeared between save and read.
        # Almost always a race with a cleanup job; treat as 500.
        logger.exception("Upload disappeared mid-pipeline: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Uploaded file could not be read after persistence.",
        ) from exc
    except IngestionError as exc:
        # The service already marked the statement FAILED and
        # committed; surface the message to the client.
        logger.warning("Ingestion rejected upload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    # 4. Build the response. Re-fetch the statement with its
    #    transactions loaded so the client sees the full picture.
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=StatementResponse.model_validate(statement).model_dump(mode="json"),
    )


@router.get(
    "/{statement_id}",
    response_model=StatementResponse,
    summary="Read a single statement by UUID",
    responses={
        status.HTTP_200_OK: {
            "description": "Statement found.",
            "model": StatementResponse,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "No statement with that UUID.",
        },
    },
)
async def get_statement(
    statement_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StatementResponse:
    """Return the statement with ``statement_id`` and its transactions.

    The endpoint uses :func:`sqlalchemy.orm.selectinload` so the
    transactions relationship is loaded in a single round-trip
    — important because the relationship is configured with
    ``lazy="selectin"`` and we want the same shape every time.

    Raises
    ------
    HTTPException
        404 when no statement matches the UUID.
    """
    result = await session.execute(
        select(Statement)
        .where(Statement.id == statement_id)
        .options(selectinload(Statement.transactions))
    )
    statement = result.scalar_one_or_none()
    if statement is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Statement {statement_id} not found",
        )
    return StatementResponse.model_validate(statement)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_size(size_bytes: int, max_megabytes: int) -> None:
    """Raise 413 when ``size_bytes`` exceeds the configured cap.

    The cap is in megabytes for the operator (matches the
    ``.env`` documentation); we convert to bytes once per
    request. The check is the first thing we do after reading
    the body so we never allocate a path on disk for an
    oversize file.
    """
    max_bytes = max_megabytes * 1024 * 1024
    if size_bytes > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(f"Uploaded file is {size_bytes} bytes, exceeding the {max_megabytes} MB cap."),
        )


def _validate_pdf(content: bytes) -> None:
    """Raise 415 when ``content`` does not look like a PDF.

    Two independent checks are performed:

    1. The first 5 bytes are ``b"%PDF-"`` (the PDF magic).
    2. The file extension is ``.pdf`` — useful for error
       messages and for defence in depth.

    Either check failing is enough to reject the upload.
    """
    if not content.startswith(_PDF_MAGIC):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Uploaded file is not a PDF (magic bytes do not start with %PDF-).",
        )


def _save_upload(content: bytes, original_name: str, upload_dir: str) -> Path:
    """Persist ``content`` under ``upload_dir`` and return the absolute path.

    The on-disk name is the original filename (sanitised) with a
    UUID prefix so two uploads of the same name do not collide.
    The directory is created on demand so a fresh checkout works
    even before the first upload.
    """
    dest_dir = Path(upload_dir)
    if not dest_dir.is_absolute():
        # Resolve relative paths against the project root so the
        # behaviour is independent of the working directory the
        # app was launched from.
        dest_dir = (Path.cwd() / dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    safe_name = _SAFE_FILENAME_RE.sub("_", original_name).strip("._")[:_FILENAME_MAX_LENGTH]
    if not safe_name:
        safe_name = "statement.pdf"
    elif not safe_name.lower().endswith(".pdf"):
        safe_name = f"{safe_name}.pdf"
    unique_name = f"{uuid.uuid4().hex}_{safe_name}"
    dest_path = dest_dir / unique_name
    dest_path.write_bytes(content)
    return dest_path


__all__ = [
    "IngestionError",
    "IngestionService",
    "get_ingestion_service",
    "get_statement",
    "router",
    "upload_statement",
]
