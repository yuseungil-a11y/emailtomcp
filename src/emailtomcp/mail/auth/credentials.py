"""`CredentialProvider` 인터페이스 (DESIGN.md §2(b) "인증 추상화").

P1은 password만 구현한다. OAuth2(M365/Outlook.com)는 P4에서 구현하며, 여기서는
`NotImplementedError`를 내는 스텁만 둔다. `accounts.auth_method`는 P0 스키마에
처음부터 있으므로 이 추상화만 P1에서 추가한다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from emailtomcp.core.errors import AuthError
from emailtomcp.secrets.keyring_store import KeyringSecretStore, get_account_password


@runtime_checkable
class CredentialProvider(Protocol):
    """계정 인증 자격증명 조회 포트."""

    def get_password(self, account_id: int) -> str:
        """비밀번호(또는 OAuth2 access token)를 돌려준다. 없으면 `AuthError`."""
        ...


class PasswordCredentialProvider:
    """`accounts.auth_method='password'`일 때 keyring에서 비밀번호를 조회한다."""

    def __init__(self, secret_store: KeyringSecretStore) -> None:
        self._secret_store = secret_store

    def get_password(self, account_id: int) -> str:
        return get_account_password(self._secret_store, account_id)


class OAuth2CredentialProvider:
    """OAuth2(M365/Outlook.com)는 P4에서 구현한다(DESIGN.md §2(b), §11.3)."""

    def get_password(self, account_id: int) -> str:
        raise NotImplementedError(
            "OAuth2 인증은 아직 구현되지 않았습니다(P4에서 구현 예정, DESIGN.md §2(b))"
        )


def credential_provider_for(
    auth_method: str, secret_store: KeyringSecretStore
) -> CredentialProvider:
    """`accounts.auth_method` 값에 맞는 `CredentialProvider`를 만든다."""
    if auth_method == "password":
        return PasswordCredentialProvider(secret_store)
    if auth_method == "oauth2":
        return OAuth2CredentialProvider()
    raise AuthError(f"알 수 없는 인증 방식입니다: {auth_method}")
