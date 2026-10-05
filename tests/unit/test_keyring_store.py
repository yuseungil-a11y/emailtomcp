"""`secrets.keyring_store` 테스트 (DESIGN.md §8.1·§8.2, 평문 폴백 없음 검증)."""

from __future__ import annotations

import keyring.errors
import pytest

from emailtomcp.core.errors import AuthError
from emailtomcp.secrets import keyring_store
from emailtomcp.secrets.keyring_store import (
    KeyringSecretStore,
    account_password_key,
    delete_account_password,
    get_account_password,
    set_account_password,
)


def test_account_password_key_format() -> None:
    assert account_password_key(42) == "acct:42:password"


def test_set_and_get_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """keyring 모듈 자체를 간단한 인메모리 딱지로 바꿔 왕복을 확인한다."""
    store_data: dict[tuple[str, str], str] = {}

    def fake_set(service: str, key: str, value: str) -> None:
        store_data[(service, key)] = value

    def fake_get(service: str, key: str) -> str | None:
        return store_data.get((service, key))

    monkeypatch.setattr(keyring_store.keyring, "set_password", fake_set)
    monkeypatch.setattr(keyring_store.keyring, "get_password", fake_get)

    store = KeyringSecretStore()
    set_account_password(store, 1, "s3cr3t")
    assert get_account_password(store, 1) == "s3cr3t"


def test_get_missing_password_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keyring_store.keyring, "get_password", lambda service, key: None)
    store = KeyringSecretStore()
    with pytest.raises(AuthError):
        get_account_password(store, 999)


def test_no_keyring_backend_raises_auth_error_not_plaintext_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """keyring 백엔드가 전혀 없으면 AuthError를 내야 한다 — 평문 폴백은 절대 없다."""

    def _raise(*args: object, **kwargs: object) -> None:
        raise keyring.errors.NoKeyringError("no backend")

    monkeypatch.setattr(keyring_store.keyring, "set_password", _raise)
    store = KeyringSecretStore()
    with pytest.raises(AuthError):
        store.set("EmailToMCP", "acct:1:password", "x")


def test_delete_missing_password_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise keyring.errors.PasswordDeleteError("not found")

    monkeypatch.setattr(keyring_store.keyring, "delete_password", _raise)
    store = KeyringSecretStore()
    delete_account_password(store, 1)  # 예외 없이 끝나야 한다


def test_get_no_keyring_backend_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise keyring.errors.NoKeyringError("no backend")

    monkeypatch.setattr(keyring_store.keyring, "get_password", _raise)
    store = KeyringSecretStore()
    with pytest.raises(AuthError, match="조회할 수 없습니다"):
        store.get("EmailToMCP", "acct:1:password")


def test_get_generic_keyring_error_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise keyring.errors.KeyringError("backend broke")

    monkeypatch.setattr(keyring_store.keyring, "get_password", _raise)
    store = KeyringSecretStore()
    with pytest.raises(AuthError, match="keyring 조회 실패"):
        store.get("EmailToMCP", "acct:1:password")


def test_set_generic_keyring_error_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise keyring.errors.KeyringError("backend broke")

    monkeypatch.setattr(keyring_store.keyring, "set_password", _raise)
    store = KeyringSecretStore()
    with pytest.raises(AuthError, match="keyring 저장 실패"):
        store.set("EmailToMCP", "acct:1:password", "x")


def test_delete_no_keyring_backend_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise keyring.errors.NoKeyringError("no backend")

    monkeypatch.setattr(keyring_store.keyring, "delete_password", _raise)
    store = KeyringSecretStore()
    with pytest.raises(AuthError, match="삭제할 수 없습니다"):
        store.delete("EmailToMCP", "acct:1:password")


def test_delete_generic_keyring_error_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise keyring.errors.KeyringError("backend broke")

    monkeypatch.setattr(keyring_store.keyring, "delete_password", _raise)
    store = KeyringSecretStore()
    with pytest.raises(AuthError, match="keyring 삭제 실패"):
        store.delete("EmailToMCP", "acct:1:password")
