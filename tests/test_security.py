import pytest
from cryptography.fernet import Fernet

from yysls_news.security.passwords import hash_password, verify_password
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


def test_password_hash_can_be_verified_without_storing_plaintext() -> None:
    encoded = hash_password("正确的管理密码")

    assert encoded != "正确的管理密码"
    assert verify_password("正确的管理密码", encoded) is True
    assert verify_password("错误的管理密码", encoded) is False
