"""Service layer for finhealth.

Service modules encapsulate the application's business logic — every
piece of orchestration that is not pure HTTP plumbing or ORM
mapping lives here. The :mod:`app.services.pdf` subpackage, for
example, houses the PDF decryption, text extraction, variant
detection, and amount-parsing pipeline that runs on every uploaded
statement.

Why services exist as their own package
---------------------------------------

* **Testability.** Pure-Python services are easy to exercise in
  isolation. The PDF pipeline can be unit-tested with real sample
  files from ``shared/`` without spinning up the FastAPI app.
* **Layering.** Routes (``app/api``) translate HTTP <-> service
  calls; services do not know about HTTP. Models (``app/models``)
  know about persistence; services know about workflows.
* **Replaceability.** A new ingestion backend (e.g. an S3 fetcher
  instead of local disk) is a service swap, not an API rewrite.

This package is intentionally empty apart from a docstring: every
concrete service is namespaced under its own subpackage
(``pdf``, ``llm`` — added in WU 3) so the import surface stays
discoverable.
"""
