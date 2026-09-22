import pytest
from cryptography.fernet import Fernet

from yysls_news.security.secrets import SecretBox, SecretConfigurationError


def test_secret_box_encrypts_and_decrypts() -> None:
    box = SecretBox(Fernet.generate_key().decode())
    value = {"sessdata": "secret", "dedeuserid": "123"}

    encrypted = box.encrypt(value)

    assert "secret" not in encrypted
    assert box.decrypt(encrypted) == value


def test_secret_box_rejects_invalid_key() -> None:
    with pytest.raises(SecretConfigurationError):
        SecretBox("invalid")
