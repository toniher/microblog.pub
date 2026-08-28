import pytest

from app.config import Config

_BASE_KWARGS = dict(
    domain="example.com",
    username="test",
    admin_password=b"hashed",
    name="test",
    summary="test",
    https=True,
    secret="secret",
)


def test_alias_url_prefix_defaults_to_post() -> None:
    config = Config.model_validate(_BASE_KWARGS)
    assert config.alias_url_prefix == "post"


def test_alias_url_prefix_rejects_a_reserved_segment() -> None:
    with pytest.raises(ValueError):
        Config.model_validate({**_BASE_KWARGS, "alias_url_prefix": "admin"})


def test_alias_url_prefix_accepts_a_custom_value() -> None:
    config = Config.model_validate({**_BASE_KWARGS, "alias_url_prefix": "blog"})
    assert config.alias_url_prefix == "blog"
