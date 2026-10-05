"""OS keyring 기반 `SecretStore` 구현 (DESIGN.md §4.6, §8.1·§8.2).

비밀번호/OAuth 토큰은 이 모듈을 통해서만 OS keyring에 저장한다. 평문 폴백은 절대
두지 않는다 — keyring 백엔드를 찾지 못하면 `AuthError`를 낸다.

키 포맷은 `acct:<account_id>:password`다(서비스명은 항상 `SERVICE_NAME`).
"""

from __future__ import annotations

import logging

import keyring
from keyring.errors import KeyringError, NoKeyringError, PasswordDeleteError

from emailtomcp.core.errors import AuthError

logger = logging.getLogger(__name__)

SERVICE_NAME = "EmailToMCP"


def account_password_key(account_id: int) -> str:
    """계정 비밀번호 키 포맷(`acct:<account_id>:password`)."""
    return f"acct:{account_id}:password"


class KeyringSecretStore:
    """`core.ports.SecretStore`의 운영 구현.

    keyring 백엔드가 전혀 없는 환경(헤드리스 Linux에 secret service가 없는 경우 등)에서는
    `keyring.errors.NoKeyringError`가 나는데, 이를 평문 폴백 없이 `AuthError`로 번역해
    "비밀번호를 저장/조회할 수 없다"는 사실을 호출자에게 명확히 알린다.
    """

    def get(self, service: str, key: str) -> str | None:
        try:
            return keyring.get_password(service, key)
        except NoKeyringError as exc:
            raise AuthError(
                "사용 가능한 OS keyring 백엔드가 없습니다. 비밀번호를 조회할 수 없습니다 "
                "(평문 저장은 지원하지 않습니다)."
            ) from exc
        except KeyringError as exc:
            raise AuthError(f"keyring 조회 실패: {exc}") from exc

    def set(self, service: str, key: str, value: str) -> None:
        try:
            keyring.set_password(service, key, value)
        except NoKeyringError as exc:
            raise AuthError(
                "사용 가능한 OS keyring 백엔드가 없습니다. 비밀번호를 저장할 수 없습니다 "
                "(평문 저장은 지원하지 않습니다)."
            ) from exc
        except KeyringError as exc:
            raise AuthError(f"keyring 저장 실패: {exc}") from exc

    def delete(self, service: str, key: str) -> None:
        try:
            keyring.delete_password(service, key)
        except PasswordDeleteError:
            # 이미 없는 키를 지우는 것은 정상 시나리오(계정 삭제 재시도 등)로 본다.
            logger.debug("keyring 삭제 대상이 이미 없습니다: service=%s key=%s", service, key)
        except NoKeyringError as exc:
            raise AuthError(
                "사용 가능한 OS keyring 백엔드가 없습니다. 비밀번호를 삭제할 수 없습니다."
            ) from exc
        except KeyringError as exc:
            raise AuthError(f"keyring 삭제 실패: {exc}") from exc


def get_account_password(store: KeyringSecretStore, account_id: int) -> str:
    """계정 비밀번호를 조회한다. 저장돼 있지 않으면 `AuthError`를 낸다."""
    value = store.get(SERVICE_NAME, account_password_key(account_id))
    if value is None:
        raise AuthError(f"계정 {account_id}의 비밀번호가 keyring에 없습니다.")
    return value


def set_account_password(store: KeyringSecretStore, account_id: int, password: str) -> None:
    """계정 비밀번호를 저장(또는 갱신)한다."""
    store.set(SERVICE_NAME, account_password_key(account_id), password)


def delete_account_password(store: KeyringSecretStore, account_id: int) -> None:
    """계정 삭제 시 비밀번호를 함께 지운다."""
    store.delete(SERVICE_NAME, account_password_key(account_id))
