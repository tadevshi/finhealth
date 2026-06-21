"""Integration tests for the web (HTML) routes.

These tests verify the Phase 0 frontend shell wired in Work Unit 5:

* ``GET /`` returns a server-rendered HTML document.
* The base template loads Tailwind (Play CDN), HTMX, and Alpine.js
  from the configured CDNs.
* Tailwind utility classes are present (proves the script tag is
  rendered and the template is valid).
* The dark-mode mechanism is in place: a pre-paint script that reads
  ``prefers-color-scheme`` and an Alpine component for the toggle.
* The app name and welcome content from ``index.html`` are rendered
  with the value from ``Settings.APP_NAME``.

The tests do *not* execute JavaScript — they assert the rendered
markup. Behavioural dark-mode coverage (e.g. clicking the toggle
flips a class) is the job of an end-to-end browser test and is out
of scope for Phase 0.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app import __version__
from app.core.config import Settings

# ---------------------------------------------------------------------------
# Constants — the substrings asserted below mirror the CDN URLs, Alpine
# component names, and Tailwind class names baked into base.html. Centralising
# them here makes it obvious if the template drifts from the test contract.
# ---------------------------------------------------------------------------

INDEX_PATH = "/"

# CDN URLs (also used in base.html — keep the test and template in sync).
HTMX_CDN = "https://unpkg.com/htmx.org@1.9.12"
ALPINE_CDN = "https://unpkg.com/alpinejs@3.14.1/dist/cdn.min.js"
TAILWIND_CDN = "https://cdn.tailwindcss.com"

# Markup markers
ALPINE_COMPONENT = "darkMode"
PREFERS_DARK_QUERY = "prefers-color-scheme: dark"
LOCALSTORAGE_KEY = "localStorage"
DARK_TOGGLE_TESTID = 'data-testid="dark-mode-toggle"'
TAILWIND_CONFIG_MARKER = "tailwind.config"
TAILWIND_DARKMODE_CLASS = "darkMode: 'class'"

# Tailwind utility classes that must appear in the rendered body.
# Picked from base.html and index.html so a typo in either file fails
# the test.
EXPECTED_TAILWIND_CLASSES = (
    "min-h-screen",  # base.html body
    "bg-white",  # base.html body
    "dark:bg-gray-900",  # base.html body
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def app_under_test(test_settings: Settings) -> AsyncIterator[FastAPI]:
    """Yield the FastAPI app instance the ``client`` fixture is bound to.

    Useful for tests that need to inspect ``app.state`` directly
    (e.g. to confirm ``settings`` is wired). The ``client`` fixture in
    :mod:`tests.conftest` is the source of truth for the HTTP layer;
    this fixture is a convenience for app-level assertions.
    """
    from app.main import create_app

    yield create_app(test_settings)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_returns_200_html(client: AsyncClient) -> None:
    """``GET /`` responds 200 with ``text/html`` content type."""
    response = await client.get(INDEX_PATH)

    assert response.status_code == 200
    content_type = response.headers["content-type"]
    assert content_type.startswith("text/html"), (
        f"Expected text/html response, got {content_type!r}"
    )


@pytest.mark.asyncio
async def test_index_contains_html5_doctype_and_viewport(client: AsyncClient) -> None:
    """The rendered document declares HTML5 and sets a mobile viewport."""
    body = (await client.get(INDEX_PATH)).text

    assert "<!DOCTYPE html>" in body
    assert 'charset="UTF-8"' in body
    assert 'name="viewport"' in body
    assert "width=device-width" in body


@pytest.mark.asyncio
async def test_index_title_uses_app_name_from_settings(
    client: AsyncClient, test_settings: Settings
) -> None:
    """The ``<title>`` reflects ``Settings.APP_NAME`` (defaults to "finhealth")."""
    body = (await client.get(INDEX_PATH)).text

    # The base template's default title is the app name; ``index.html``
    # extends it with " &mdash; Home". Both forms should be present.
    assert test_settings.APP_NAME in body
    assert "<title>" in body
    assert "</title>" in body


# ---------------------------------------------------------------------------
# Frontend stack: HTMX, Alpine.js, Tailwind CSS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_loads_htmx_via_cdn(client: AsyncClient) -> None:
    """A ``<script>`` tag references the HTMX CDN URL."""
    body = (await client.get(INDEX_PATH)).text

    assert HTMX_CDN in body, (
        f"HTMX CDN URL {HTMX_CDN!r} not found in rendered body. Check app/web/templates/base.html."
    )
    # Make sure the script tag is opened, not just the URL as text.
    assert f'src="{HTMX_CDN}"' in body


@pytest.mark.asyncio
async def test_index_loads_alpine_via_cdn(client: AsyncClient) -> None:
    """A ``<script defer>`` tag references the Alpine.js CDN URL."""
    body = (await client.get(INDEX_PATH)).text

    assert ALPINE_CDN in body
    # ``defer`` is required so Alpine boots after the DOM is parsed.
    assert f'defer src="{ALPINE_CDN}"' in body


@pytest.mark.asyncio
async def test_index_loads_tailwind_via_cdn(client: AsyncClient) -> None:
    """A ``<script>`` tag references the Tailwind Play CDN URL."""
    body = (await client.get(INDEX_PATH)).text

    assert TAILWIND_CDN in body
    assert f'src="{TAILWIND_CDN}"' in body


@pytest.mark.asyncio
async def test_index_configures_tailwind_class_based_dark_mode(
    client: AsyncClient,
) -> None:
    """Tailwind is configured with ``darkMode: 'class'`` so ``dark:`` utilities work."""
    body = (await client.get(INDEX_PATH)).text

    assert TAILWIND_CONFIG_MARKER in body
    assert TAILWIND_DARKMODE_CLASS in body, (
        "Tailwind must be configured with class-based dark mode so the "
        "`dark:` prefix on utility classes flips with the `dark` class "
        "on <html>."
    )


@pytest.mark.asyncio
async def test_index_renders_tailwind_utility_classes(client: AsyncClient) -> None:
    """Tailwind utility classes are present in the rendered markup.

    This is a smoke test that proves the template renders without
    syntax errors and that the Tailwind script tag is wired
    (otherwise a misconfigured template would still "work" but render
    no styling).
    """
    body = (await client.get(INDEX_PATH)).text

    missing = [cls for cls in EXPECTED_TAILWIND_CLASSES if cls not in body]
    assert not missing, f"Tailwind utility classes missing from body: {missing}"


# ---------------------------------------------------------------------------
# Dark mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_has_pre_paint_dark_mode_script(client: AsyncClient) -> None:
    """A pre-paint script in ``<head>`` reads ``prefers-color-scheme``.

    Without this script the page would flash the default (light) theme
    before Alpine booted and applied the ``dark`` class. The script
    must run *before* the body paints, so it lives in ``<head>`` and
    uses the synchronous pattern documented in base.html.
    """
    body = (await client.get(INDEX_PATH)).text

    # Order matters: the pre-paint script must come before Tailwind/HTMX
    # so the ``dark`` class is set before any utility class is computed.
    head_close = body.find("</head>")
    assert head_close > 0
    head = body[:head_close]

    assert PREFERS_DARK_QUERY in head, (
        "Pre-paint dark-mode script not found in <head>. "
        "It must query matchMedia('(prefers-color-scheme: dark)') "
        "before the body paints."
    )
    assert LOCALSTORAGE_KEY in head, (
        "Pre-paint dark-mode script must also read localStorage so a "
        "user's saved preference wins over the system preference."
    )


@pytest.mark.asyncio
async def test_index_registers_alpine_dark_mode_component(client: AsyncClient) -> None:
    """The ``darkMode`` Alpine component is registered via ``alpine:init``."""
    body = (await client.get(INDEX_PATH)).text

    assert "alpine:init" in body, (
        "darkMode Alpine component must be registered on the "
        "`alpine:init` event so it is available before any "
        '`x-data="darkMode"` binding on the page is evaluated.'
    )
    assert ALPINE_COMPONENT in body
    # The component must expose a ``toggle`` action bound to the
    # header button.
    assert "toggle" in body


@pytest.mark.asyncio
async def test_index_renders_dark_mode_toggle_button(client: AsyncClient) -> None:
    """The header contains a dark-mode toggle button with Alpine bindings."""
    body = (await client.get(INDEX_PATH)).text

    assert DARK_TOGGLE_TESTID in body, (
        "Header must render a button with data-testid='dark-mode-toggle'."
    )
    # The button is bound to the Alpine toggle action.
    assert "@click" in body
    # And it lives inside the header element.
    header_start = body.find("<header")
    header_end = body.find("</header>")
    assert header_start > 0 and header_end > header_start
    assert DARK_TOGGLE_TESTID in body[header_start:header_end]


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_renders_welcome_message(client: AsyncClient) -> None:
    """The index page contains the Phase 0 welcome content."""
    body = (await client.get(INDEX_PATH)).text

    assert "Welcome to" in body
    assert "Phase 0" in body
    # The dashboard placeholder is signalled explicitly so testers
    # can find it from the rendered page.
    assert "Phase 3" in body


@pytest.mark.asyncio
async def test_index_renders_header_and_footer(client: AsyncClient) -> None:
    """The base layout renders both ``<header>`` and ``<footer>`` elements."""
    body = (await client.get(INDEX_PATH)).text

    assert "<header" in body and "</header>" in body
    assert "<footer" in body and "</footer>" in body


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_uses_settings_app_name(
    client: AsyncClient, test_settings: Settings, app_under_test: FastAPI
) -> None:
    """The page reflects the configured ``APP_NAME`` end-to-end.

    This guards against the template hard-coding "finhealth" — the
    value must come from ``app.state.settings.APP_NAME`` so a custom
    name in ``.env`` is honoured.
    """
    body = (await client.get(INDEX_PATH)).text

    assert test_settings.APP_NAME in body
    # And the app instance really did receive the test settings.
    assert app_under_test.state.settings.APP_NAME == test_settings.APP_NAME


@pytest.mark.asyncio
async def test_index_html_matches_version_agnostic(
    client: AsyncClient,
) -> None:
    """The rendered page does not include the Python ``__version__`` string.

    Versioning belongs in the API (the ``/health`` endpoint) and in the
    OpenAPI document, not in the HTML body. This test pins that
    contract: a future change that accidentally leaks the Python
    version into the template will fail loudly.
    """
    body = (await client.get(INDEX_PATH)).text

    assert __version__ not in body


# ---------------------------------------------------------------------------
# File-level sanity check (cheap regression guard)
# ---------------------------------------------------------------------------


def test_templates_directory_contains_base_and_index() -> None:
    """The template files exist on disk where the router expects them.

    This is a path-level guard: if someone moves or renames the
    templates the router's ``Jinja2Templates`` initialisation would
    raise at import time, but a static analysis run (or a developer
    reading the file tree) gets an early signal.
    """
    # Resolve from this file's location — same convention as the
    # router — so the test does not depend on the working directory.
    templates_dir = Path(__file__).resolve().parent.parent / "app" / "web" / "templates"

    assert templates_dir.is_dir(), f"Templates directory missing: {templates_dir}"
    assert (templates_dir / "base.html").is_file(), "base.html missing"
    assert (templates_dir / "index.html").is_file(), "index.html missing"
