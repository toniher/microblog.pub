import re
from functools import lru_cache
from pathlib import Path
from typing import Callable

import babel.support
from babel import negotiate_locale as _babel_negotiate_locale
from fastapi import Request
from jinja2.ext import _make_new_gettext
from jinja2.ext import _make_new_ngettext

from app import config

ROOT_DIR = Path().parent.resolve()

# `data/` overrides `app/`, mirroring the existing template/asset override
# convention (see `app/templates.py`'s `Jinja2Templates(directory=[...])`).
TRANSLATIONS_DIRS = [
    ROOT_DIR / "data" / "translations",
    ROOT_DIR / "app" / "translations",
]

DOMAIN = "messages"


def _discover_locales() -> set[str]:
    locales = {"en", config.LANGUAGE_CODE}
    for base_dir in TRANSLATIONS_DIRS:
        if not base_dir.is_dir():
            continue
        for entry in base_dir.iterdir():
            if (entry / "LC_MESSAGES" / f"{DOMAIN}.mo").exists():
                locales.add(entry.name)
    return locales


AVAILABLE_LOCALES = _discover_locales()


@lru_cache(maxsize=None)
def get_translations(locale: str) -> babel.support.NullTranslations:
    """Load the catalog for `locale`, `data/` over `app/`, falling back to English."""
    translations: babel.support.NullTranslations = babel.support.NullTranslations()
    # Load in lowest-to-highest priority order, chaining each one as a
    # fallback for the next, so a `data/` catalog missing some msgids still
    # falls back to the bundled `app/` catalog, then to the source strings.
    for base_dir in reversed(TRANSLATIONS_DIRS):
        if not base_dir.is_dir():
            continue
        loaded = babel.support.Translations.load(
            str(base_dir), locales=[locale], domain=DOMAIN
        )
        if isinstance(loaded, babel.support.Translations):
            loaded.add_fallback(translations)
            translations = loaded
    return translations


_ACCEPT_LANGUAGE_RE = re.compile(r"^\s*([a-zA-Z-]+)\s*(?:;\s*q\s*=\s*([0-9.]+))?\s*$")


def _parse_accept_language(header: str) -> list[str]:
    parsed = []
    for part in header.split(","):
        match = _ACCEPT_LANGUAGE_RE.match(part)
        if not match:
            continue
        tag, q = match.groups()
        parsed.append((tag, float(q) if q else 1.0))
    parsed.sort(key=lambda pair: pair[1], reverse=True)
    return [tag.replace("-", "_") for tag, _ in parsed]


def negotiate_locale(accept_language: str | None) -> str:
    if accept_language:
        preferred = _parse_accept_language(accept_language)
        negotiated = _babel_negotiate_locale(preferred, AVAILABLE_LOCALES)
        if negotiated:
            return negotiated
    return config.LANGUAGE_CODE


def resolve_locale(request: Request) -> str:
    """Public pages negotiate `Accept-Language`; the admin UI always uses
    the instance's configured `language_code`."""
    if request.url.path.startswith("/admin"):
        return config.LANGUAGE_CODE
    return negotiate_locale(request.headers.get("accept-language"))


def get_jinja_i18n_callables(
    locale: str,
) -> tuple[Callable, Callable]:
    """`{% trans %}`/`{% pluralize %}` need Jinja's "newstyle" gettext/ngettext
    wrappers (they accept the extra `**variables` the compiled trans block
    passes for interpolation) rather than the raw `Translations` methods."""
    translations = get_translations(locale)
    return _make_new_gettext(translations.gettext), _make_new_ngettext(
        translations.ngettext
    )


def gettext_default(message: str) -> str:
    """For user-facing strings raised outside a request/template context
    (e.g. dependency-level `HTTPException`s), translated at the instance locale."""
    return get_translations(config.LANGUAGE_CODE).gettext(message)
