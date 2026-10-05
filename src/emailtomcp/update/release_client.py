"""매니페스트 다운로드용 `HttpClient` 구현 (DESIGN.md §4.6, §3.4, §14.4).

- 운영 구현은 httpx 계열 클라이언트(+ truststore로 OS 인증서 저장소 사용)다. 이 환경의
  `mcp` 의존성은 httpx 포크인 `httpx2`(같은 API)를 끌어오므로 `httpx` → `httpx2` 순으로
  찾아 쓴다.
- `base_url`은 주입 가능하다(테스트는 `tests/fakes/fake_github_releases.py`를 가리킨다).
  운영 기본값은 GitHub Pages(`DEFAULT_BASE_URL`)이고, 덮어쓰기는 **dev 빌드에서만** 받는다
  (app.py가 판단, §4.6).
- 응답 본문은 **스트리밍으로 받으면서 상한(기본 64KB)을 넘는 순간 중단**한다 — 거대한
  응답으로 메모리를 소진시키는 공격을 막는다(§14.4 [1]). 서명 파일(`.minisig`)은 상한을
  별도로 2KB로 둔다(보안검토 M-2).
- httpx의 timeout은 읽기 1회당 상한이라, 느리게 조금씩 보내는 서버가 작업 스레드(와
  `Backend.stop()`의 executor 종료)를 무기한 붙잡을 수 있다. 그래서 요청 1건당 **벽시계 기준
  전체 상한(기본 30초)**을 두고, 넘으면 끊고 `TransientError`를 낸다(보안검토 M-2).
- GitHub 토큰은 쓰지 않는다(K3). GitHub API도 호출하지 않는다(§14.2 — 메타데이터 불신).
- 이 클라이언트를 신뢰 근거로 쓰지 않는다. 신뢰는 오직 서명 검증(`update.keys`)으로 판단한다.
"""

from __future__ import annotations

import logging
import ssl
import time
from dataclasses import dataclass
from types import ModuleType
from typing import Any
from urllib.parse import urljoin, urlsplit

from emailtomcp.core.errors import PermanentError, TransientError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://yuseungil-a11y.github.io/emailtomcp/manifest/"
MANIFEST_PATH = "stable.json"
SIGNATURE_PATH = "stable.json.minisig"
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_MAX_SIGNATURE_BYTES = 2 * 1024
SIGNATURE_SUFFIX = ".minisig"
DEFAULT_TIMEOUT_SEC = 10.0
DEFAULT_TOTAL_TIMEOUT_SEC = 30.0
_MAX_REDIRECTS = 3
_USER_AGENT_PREFIX = "EmailToMCP-UpdateCheck"


class ResponseTooLarge(PermanentError):
    """응답 본문이 상한을 넘었다(§14.4 [1])."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    content: bytes
    url: str


def _load_httpx() -> ModuleType:
    # importlib가 아니라 정적 import로 쓴다 — PyInstaller가 의존성을 분석해 번들에 넣게 하려는 것.
    try:
        import httpx

        return httpx
    except ImportError:
        pass
    try:
        import httpx2

        return httpx2
    except ImportError as exc:
        raise TransientError("HTTP 클라이언트 라이브러리(httpx)를 찾을 수 없습니다") from exc


def _ssl_context() -> ssl.SSLContext | bool:
    """truststore가 있으면 OS 인증서 저장소를 쓰는 SSLContext, 없으면 기본 검증(True)."""
    try:
        import truststore
    except ImportError:
        return True
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class HttpxReleaseClient:
    """`core.ports.HttpClient` 구현. `get(path)`는 `HttpResponse`를 돌려준다.

    네트워크 오류·타임아웃·5xx·전체 시간 상한 초과는 `TransientError`, 크기 상한 초과는
    `ResponseTooLarge`.
    4xx는 오류로 올리지 않고 `status_code`로 돌려준다(호출자가 404를 "파일 없음"으로 처리).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_signature_bytes: int = DEFAULT_MAX_SIGNATURE_BYTES,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT_SEC,
        user_agent_version: str = "unknown",
    ) -> None:
        if not base_url.endswith("/"):
            base_url += "/"
        self.base_url = base_url
        self.max_response_bytes = max_response_bytes
        self.max_signature_bytes = max_signature_bytes
        self.total_timeout = total_timeout
        self._user_agent = f"{_USER_AGENT_PREFIX}/{user_agent_version}"
        self._require_https = urlsplit(base_url).scheme == "https"

    def _url_for(self, path: str) -> str:
        url = urljoin(self.base_url, path)
        if not url.startswith(self.base_url):
            raise PermanentError("base_url 밖의 경로는 요청할 수 없습니다")
        return url

    def _limit_for(self, path: str) -> int:
        if path.endswith(SIGNATURE_SUFFIX):
            return min(self.max_signature_bytes, self.max_response_bytes)
        return self.max_response_bytes

    def get(self, path: str, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Any:
        httpx: Any = _load_httpx()
        url = self._url_for(path)
        limit = self._limit_for(path)
        deadline = time.monotonic() + self.total_timeout

        def check_deadline() -> None:
            if time.monotonic() > deadline:
                raise TransientError(
                    f"응답이 너무 느려 중단했습니다(전체 {self.total_timeout:g}초 초과)"
                )

        try:
            with (
                httpx.Client(
                    verify=_ssl_context(),
                    timeout=timeout,
                    follow_redirects=True,
                    max_redirects=_MAX_REDIRECTS,
                    headers={"User-Agent": self._user_agent, "Accept-Encoding": "identity"},
                ) as client,
                client.stream("GET", url) as response,
            ):
                check_deadline()
                final_url = str(response.url)
                if self._require_https and urlsplit(final_url).scheme != "https":
                    raise PermanentError("HTTPS가 아닌 주소로 리다이렉트되었습니다")
                if response.status_code >= 500:
                    raise TransientError(f"서버 오류: HTTP {response.status_code}")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > limit:
                    raise ResponseTooLarge("응답 크기가 상한을 넘습니다")
                chunks: list[bytes] = []
                total = 0
                # 압축 해제 후 바이트 수로 센다(압축 폭탄 방어). 서명은 해제된 본문 기준이다.
                # 청크마다 벽시계 상한을 확인한다(느린 전송으로 무기한 붙잡히는 것 방지, M-2).
                for chunk in response.iter_bytes():
                    check_deadline()
                    total += len(chunk)
                    if total > limit:
                        raise ResponseTooLarge("응답 크기가 상한을 넘습니다")
                    chunks.append(chunk)
                check_deadline()
                return HttpResponse(
                    status_code=response.status_code, content=b"".join(chunks), url=final_url
                )
        except (TransientError, PermanentError):
            raise
        except httpx.HTTPError as exc:
            raise TransientError(f"네트워크 오류: {type(exc).__name__}") from exc
        except OSError as exc:
            raise TransientError(f"네트워크 오류: {type(exc).__name__}") from exc
