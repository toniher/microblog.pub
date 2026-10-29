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


@pytest.mark.parametrize("prefix", ["admin", "oauth", "api", "micropub"])
def test_alias_url_prefix_rejects_a_reserved_segment(prefix: str) -> None:
    with pytest.raises(ValueError):
        Config.model_validate({**_BASE_KWARGS, "alias_url_prefix": prefix})


def test_alias_url_prefix_accepts_a_custom_value() -> None:
    config = Config.model_validate({**_BASE_KWARGS, "alias_url_prefix": "blog"})
    assert config.alias_url_prefix == "blog"


def test_verify_password_handles_passwords_over_72_bytes(monkeypatch) -> None:
    import bcrypt

    from app import config

    long_pwd = "x" * 80
    # A hash made by bcrypt < 5, which silently used the first 72 bytes.
    hashed = bcrypt.hashpw(long_pwd.encode()[:72], bcrypt.gensalt())
    monkeypatch.setattr(config.CONFIG, "admin_password", hashed)

    assert config.verify_password(long_pwd)
    assert not config.verify_password("y" * 80)
