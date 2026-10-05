"""업데이트 테스트용 가짜 GitHub Pages/Releases 서버 + 테스트 서명 도구 (DESIGN.md §12.4).

- `FakeGithubReleases`: 127.0.0.1 랜덤 포트에서 `/manifest/stable.json`과
  `/manifest/stable.json.minisig`를 서빙하는 작은 HTTP 서버. 응답 본문/상태코드를 테스트에서
  바꿀 수 있다. `base_url`을 `HttpxReleaseClient`에 주입해 쓴다.
- `TestSigningKey`: **테스트 전용** Ed25519 키쌍. 고정 시드 문자열에서 결정적으로 만든다
  (`cryptography` Ed25519 API). 실제 minisign과 같은 형식의 공개키 줄·서명 파일을 만든다.
  - 이 키들은 공개 저장소에 있는 시드로 누구나 재현할 수 있으므로 **절대 운영 빌드에 내장하면
    안 된다**(`update/keys.py` 모듈 docstring, `test_update_keys.py`의 가드 테스트 참고).
- `build_manifest()` / `encode_manifest()`: §14.4 예시와 같은 매니페스트를 만들고, 키 정렬·LF·
  UTF-8로 직렬화한다.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from emailtomcp.update.keys import KeyRole, TrustedKey, parse_public_key

RELEASES = "https://github.com/yuseungil-a11y/emailtomcp/releases/"


@dataclass(frozen=True)
class TestSigningKey:
    """테스트 전용 minisign 호환 Ed25519 키."""

    __test__ = False  # pytest가 테스트 클래스로 수집하지 않게 한다

    label: str
    private_key: Ed25519PrivateKey
    key_id: bytes

    @classmethod
    def from_seed(cls, label: str) -> TestSigningKey:
        seed = hashlib.sha256(f"emailtomcp-TEST-ONLY-signing-key:{label}".encode()).digest()
        key_id = hashlib.sha256(f"emailtomcp-TEST-ONLY-key-id:{label}".encode()).digest()[:8]
        return cls(
            label=label,
            private_key=Ed25519PrivateKey.from_private_bytes(seed),
            key_id=key_id,
        )

    def public_key_line(self) -> str:
        raw_pk = self.private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return base64.b64encode(b"Ed" + self.key_id + raw_pk).decode("ascii")

    def public_key_file(self) -> str:
        return (
            f"untrusted comment: minisign public key {self.key_id[::-1].hex().upper()}\n"
            f"{self.public_key_line()}\n"
        )

    def trusted_key(self, role: KeyRole = "active") -> TrustedKey:
        return parse_public_key(self.public_key_line(), label=self.label, role=role)

    def sign(
        self,
        message: bytes,
        trusted_comment: str,
        *,
        prehash: bool = True,
        key_id: bytes | None = None,
    ) -> bytes:
        """minisign 형식 서명 파일(4줄)을 만든다. `prehash=False`면 레거시 `Ed`."""
        algorithm = b"ED" if prehash else b"Ed"
        payload = hashlib.blake2b(message, digest_size=64).digest() if prehash else message
        signature = self.private_key.sign(payload)
        global_sig = self.private_key.sign(signature + trusted_comment.encode("utf-8"))
        sig_line = base64.b64encode(algorithm + (key_id or self.key_id) + signature).decode()
        return (
            "untrusted comment: signature from minisign secret key (TEST)\n"
            f"{sig_line}\n"
            f"trusted comment: {trusted_comment}\n"
            f"{base64.b64encode(global_sig).decode()}\n"
        ).encode()


TEST_KEY_ACTIVE = TestSigningKey.from_seed("K1-test")
TEST_KEY_STANDBY = TestSigningKey.from_seed("K2-test")
TEST_KEY_UNTRUSTED = TestSigningKey.from_seed("rogue")


def test_trusted_keys() -> tuple[TrustedKey, ...]:
    """테스트에서 '내장 키'로 주입할 목록(활성 K1 + 예비 K2)."""
    return (TEST_KEY_ACTIVE.trusted_key("active"), TEST_KEY_STANDBY.trusted_key("standby"))


test_trusted_keys.__test__ = False  # type: ignore[attr-defined]


def _artifact(
    version: str,
    kind: str,
    name: str,
    *,
    platform: str = "windows",
    arch: str = "x64",
    digest_seed: str = "",
) -> dict[str, Any]:
    return {
        "platform": platform,
        "arch": arch,
        "kind": kind,
        "name": name,
        "url": f"{RELEASES}download/v{version}/{name}",
        "size": 1024,
        "sha256": hashlib.sha256(f"{version}:{name}:{digest_seed}".encode()).hexdigest(),
    }


def build_manifest(
    *,
    version: str = "1.0.1",
    issued_at: str = "2026-11-01T00:00:00Z",
    expires: str = "2026-12-01T00:00:00Z",
    floor: str = "1.0.0",
    security: bool = False,
    app_id: str = "EmailToMCP",
    channel: str = "stable",
    digest_seed: str = "",
    release_page: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "app_id": app_id,
        "channel": channel,
        "issued_at": issued_at,
        "expires": expires,
        "min_version_floor": floor,
        "latest": {
            "version": version,
            "released_at": "2026-10-31T09:00:00Z",
            "security": security,
            "release_page": release_page or f"{RELEASES}tag/v{version}",
            "notes_summary": "버그 수정",
            "artifacts": [
                _artifact(version, "setup", "EmailToMCP-win-Setup.exe", digest_seed=digest_seed),
                _artifact(
                    version,
                    "velopack_full",
                    f"EmailToMCP-{version}-full.nupkg",
                    digest_seed=digest_seed,
                ),
            ],
        },
    }


def encode_manifest(manifest: dict[str, Any]) -> bytes:
    """§14.4 형식: UTF-8 JSON, 키 정렬, LF."""
    return (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def trusted_comment_for(manifest: dict[str, Any]) -> str:
    return (
        f"app={manifest['app_id']} channel={manifest['channel']} "
        f"version={manifest['latest']['version']} issued={manifest['issued_at']}"
    )


def sign_manifest(
    manifest: dict[str, Any],
    key: TestSigningKey = TEST_KEY_ACTIVE,
    *,
    prehash: bool = True,
    trusted_comment: str | None = None,
) -> tuple[bytes, bytes]:
    """(매니페스트 바이트, 서명 파일 바이트)."""
    data = encode_manifest(manifest)
    comment = trusted_comment if trusted_comment is not None else trusted_comment_for(manifest)
    return data, key.sign(data, comment, prehash=prehash)


def clone(manifest: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(manifest)


# ---------------------------------------------------------------------------
# HTTP 서버
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def do_GET(self) -> None:  # noqa: N802 — http.server 규약
        self.server.request_log.append(self.path)
        entry = self.server.routes.get(self.path)
        if entry is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status, body = entry
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        drip = self.server.drip_interval
        if drip <= 0:
            self.wfile.write(body)
            return
        # 느리게 조금씩 보내는 서버 흉내(보안검토 M-2: 전체 시간 상한 검증용)
        try:
            for i in range(len(body)):
                self.wfile.write(body[i : i + 1])
                self.wfile.flush()
                time.sleep(drip)
        except OSError:
            pass  # 클라이언트가 상한에 걸려 끊은 경우

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — 조용히
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.request_log: list[str] = []
        self.drip_interval: float = 0.0


class FakeGithubReleases:
    """`with FakeGithubReleases() as fake:` — `fake.base_url`을 클라이언트에 주입한다."""

    MANIFEST = "/manifest/stable.json"
    SIGNATURE = "/manifest/stable.json.minisig"

    def __init__(self) -> None:
        self._server = _Server()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}/manifest/"

    @property
    def request_log(self) -> list[str]:
        return self._server.request_log

    def publish(self, manifest_bytes: bytes | None, signature_bytes: bytes | None) -> None:
        """None을 주면 해당 파일은 404가 된다."""
        routes = self._server.routes
        routes.pop(self.MANIFEST, None)
        routes.pop(self.SIGNATURE, None)
        if manifest_bytes is not None:
            routes[self.MANIFEST] = (200, manifest_bytes)
        if signature_bytes is not None:
            routes[self.SIGNATURE] = (200, signature_bytes)

    def set_drip(self, interval_sec: float) -> None:
        """0보다 크면 본문을 1바이트씩 `interval_sec` 간격으로 보낸다."""
        self._server.drip_interval = interval_sec

    def set_status(self, path: str, status: int, body: bytes = b"") -> None:
        self._server.routes[path] = (status, body)

    def __enter__(self) -> FakeGithubReleases:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
