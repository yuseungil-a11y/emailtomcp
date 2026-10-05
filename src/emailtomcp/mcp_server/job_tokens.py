"""잡 토큰 저장소 — `core.ports.JobTokenIssuer` 구현 (DESIGN.md §2(c) H4, §7.10, §8.3) — P3 Phase B.

- **메모리 전용**, 대화형 토큰 저장소(`mcp_tokens` 테이블, auth.TokenStore)와 완전히 분리한다
  (§7.10 "리뷰 11번 6항" 행). DB에 쓰지 않는다.
- 형식: `JOB_TOKEN_PREFIX` + `token_urlsafe(32)`. 접두사의 `.`은 urlsafe base64 알파벳에 없는
  문자라 **대화형 토큰은 이 접두사로 시작할 수 없다** → 가드는 접두사만 보고 두 저장소 중
  **한 곳만** 조회한다(보안리뷰 L-D: 한쪽 실패 시 다른 쪽을 다시 찾는 폴백 조회 없음).
- 잡 하나에 토큰 하나. 발급은 러너가 G4 커밋 뒤에 하고, 제출·타임아웃·강제종료·끄기 때 즉시
  폐기한다. 비교는 sha256 + `hmac.compare_digest`.
- 인증 결과 주체는 `Principal(kind="job", scopes={SCOPE_JOB}, job_id=...)`다. "잡이 running일 때만
  유효"는 도구 핸들러가 DB로 한 번 더 확인한다(tools_job.py).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading

from emailtomcp.core.models import McpAuditPrincipal
from emailtomcp.mcp_server.auth import MAX_PRESENTED_TOKEN_LEN, Principal
from emailtomcp.mcp_server.scopes import SCOPE_JOB

JOB_TOKEN_PREFIX = "ejob."


def is_job_token_format(presented: str) -> bool:
    return presented.startswith(JOB_TOKEN_PREFIX)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class JobTokenStore:
    """잡 토큰 발급·검증·폐기(스레드 안전, 메모리 전용)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_job: dict[int, str] = {}  # job_id -> sha256
        self._by_hash: dict[str, int] = {}  # sha256 -> job_id

    def issue(self, job_id: int) -> str:
        job_id = int(job_id)
        if job_id < 1:
            raise ValueError("잡 ID가 올바르지 않습니다")
        plaintext = JOB_TOKEN_PREFIX + secrets.token_urlsafe(32)
        digest = _digest(plaintext)
        with self._lock:
            old = self._by_job.pop(job_id, None)
            if old is not None:
                self._by_hash.pop(old, None)
            self._by_job[job_id] = digest
            self._by_hash[digest] = job_id
        return plaintext

    def revoke(self, job_id: int) -> None:
        with self._lock:
            digest = self._by_job.pop(int(job_id), None)
            if digest is not None:
                self._by_hash.pop(digest, None)

    def revoke_all(self) -> None:
        with self._lock:
            self._by_job.clear()
            self._by_hash.clear()

    def active_job_ids(self) -> frozenset[int]:
        with self._lock:
            return frozenset(self._by_job)

    def authenticate(self, presented: str) -> Principal | None:
        if (
            not presented
            or len(presented) > MAX_PRESENTED_TOKEN_LEN
            or not is_job_token_format(presented)
        ):
            return None
        digest = _digest(presented)
        with self._lock:
            job_id = self._by_hash.get(digest)
            stored = self._by_job.get(job_id) if job_id is not None else None
        if job_id is None or stored is None or not hmac.compare_digest(stored, digest):
            return None
        return Principal(
            kind=McpAuditPrincipal.JOB,
            scopes=frozenset({SCOPE_JOB}),
            token_hash8=digest[:8],
            job_id=job_id,
        )
