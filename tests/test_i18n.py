from pathlib import Path
from typing import Generator

import pytest
from babel.messages.catalog import Catalog
from babel.messages.mofile import write_mo
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import i18n as app_i18n


@pytest.fixture
def fr_catalog(tmp_path: Path) -> Generator[None, None, None]:
    mo_dir = tmp_path / "fr" / "LC_MESSAGES"
    mo_dir.mkdir(parents=True)

    catalog = Catalog(locale="fr")
    catalog.add("Skip to content", "Passer au contenu")
    with open(mo_dir / "messages.mo", "wb") as f:
        write_mo(f, catalog)

    original_dirs = app_i18n.TRANSLATIONS_DIRS
    original_locales = app_i18n.AVAILABLE_LOCALES
    app_i18n.TRANSLATIONS_DIRS = [tmp_path]
    app_i18n.AVAILABLE_LOCALES = {"en", "fr"}
    app_i18n.get_translations.cache_clear()
    try:
        yield
    finally:
        app_i18n.TRANSLATIONS_DIRS = original_dirs
        app_i18n.AVAILABLE_LOCALES = original_locales
        app_i18n.get_translations.cache_clear()


def test_public_page_negotiates_accept_language(
    client: TestClient, db: Session, fr_catalog: None
) -> None:
    response = client.get("/", headers={"Accept-Language": "fr"})
    assert response.status_code == 200
    assert 'lang="fr"' in response.text
    assert "Passer au contenu" in response.text


def test_public_page_falls_back_to_english_by_default(
    client: TestClient, db: Session, fr_catalog: None
) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert 'lang="en"' in response.text
    assert "Skip to content" in response.text


def test_public_page_falls_back_for_unavailable_locale(
    client: TestClient, db: Session, fr_catalog: None
) -> None:
    response = client.get("/", headers={"Accept-Language": "de"})
    assert response.status_code == 200
    assert 'lang="en"' in response.text
    assert "Skip to content" in response.text


def test_admin_route_ignores_accept_language(
    client: TestClient, db: Session, fr_catalog: None
) -> None:
    # The admin UI always renders in the instance's configured `language_code`,
    # regardless of the visitor's `Accept-Language` header.
    response = client.get("/admin/login", headers={"Accept-Language": "fr"})
    assert response.status_code == 200
    assert 'lang="en"' in response.text
    assert "Skip to content" in response.text
    assert "Passer au contenu" not in response.text
