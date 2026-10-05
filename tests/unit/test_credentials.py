"""`mail.auth.credentials` 테스트 (DESIGN.md §2(b) 인증 추상화)."""

from __future__ import annotations

import pytest

import emailtomcp.secrets.keyring_store as keyring_store
from emailtomcp.core.errors import AuthError
from emailtomcp.mail.auth.credentials import (
    OAuth2CredentialProvider,
    PasswordCredentialProvider,
    credential_provider_for,
)
from emailtomcp.secrets.keyring_store import KeyringSecretStore


def test_password_provider_reads_from_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        keyring_store.keyring,
        "get_password",
        lambda service, key: "s3cr3t" if key == "acct:5:password" else None,
    )
    provider = PasswordCredentialProvider(KeyringSecretStore())
    assert provider.get_password(5) == "s3cr3t"


def test_oauth2_provider_not_implemented() -> None:
    provider = OAuth2CredentialProvider()
    with pytest.raises(NotImplementedError):
        provider.get_password(1)


def test_credential_provider_for_dispatches_by_auth_method() -> None:
    store = KeyringSecretStore()
    assert isinstance(credential_provider_for("password", store), PasswordCredentialProvider)
    assert isinstance(credential_provider_for("oauth2", store), OAuth2CredentialProvider)


def test_credential_provider_for_unknown_method_raises_auth_error() -> None:
    store = KeyringSecretStore()
    with pytest.raises(AuthError):
        credential_provider_for("carrier-pigeon", store)
