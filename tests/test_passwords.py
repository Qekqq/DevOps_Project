import pytest

from src.passwords import hash_password, verify_password


def test_password_hash_is_salted_and_verifiable():
    password = "Тестовый пароль для проверки"
    first = hash_password(password)
    assert first != hash_password(password)
    assert password not in first
    assert len(first) <= 255
    assert verify_password(password, first)
    assert not verify_password("Другой тестовый пароль", first)


@pytest.mark.parametrize("encoded", [None, "", "plain password", "scrypt$1$8$1$00$00"])
def test_invalid_hash_is_rejected(encoded):
    assert not verify_password("Тестовый пароль для проверки", encoded)


@pytest.mark.parametrize("password", ["", "short", "a" * 129])
def test_invalid_password_length(password):
    with pytest.raises(ValueError):
        hash_password(password)
