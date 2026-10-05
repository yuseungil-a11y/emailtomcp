"""POP3 통합 테스트용 가짜 서버 (갈릴레오 §2 "fake POP3/IMAP 서버").

완벽한 RFC 1939 구현이 아니라, `Pop3IncomingProvider`가 실제로 쓰는 최소 명령
(USER/PASS/STAT/LIST/UIDL/RETR/DELE/QUIT)만 흉내낸다. 평문(`security=none`)으로만
동작한다 — STARTTLS(STLS) 시나리오는 이 fake로는 다루지 않는다(실 서버 인증서가
필요해 통합 테스트 범위 밖으로 둔다).
"""

from __future__ import annotations

import socket
import socketserver
import threading


class _Mailbox:
    def __init__(self) -> None:
        # (uidl, raw_bytes) 목록. 인덱스 + 1이 POP3 메시지 번호다.
        self.messages: list[tuple[str, bytes]] = []
        self.deleted: set[int] = set()

    def add(self, uidl: str, raw: bytes) -> None:
        self.messages.append((uidl, raw))


class _Pop3Handler(socketserver.StreamRequestHandler):
    server: FakePop3Server

    def handle(self) -> None:
        self._send(b"+OK fake pop3 ready")
        authenticated = False
        username = None
        while True:
            line = self.rfile.readline()
            if not line:
                break
            line = line.rstrip(b"\r\n")
            if not line:
                continue
            parts = line.decode("ascii", errors="replace").split(" ", 1)
            cmd = parts[0].upper()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd == "USER":
                username = arg
                self._send(b"+OK")
            elif cmd == "PASS":
                if username == self.server.username and arg == self.server.password:
                    authenticated = True
                    self._send(b"+OK logged in")
                else:
                    self._send(b"-ERR invalid credentials")
            elif cmd == "STAT":
                if not authenticated:
                    self._send(b"-ERR not authenticated")
                    continue
                active = [
                    m
                    for i, m in enumerate(self.server.mailbox.messages, 1)
                    if i not in self.server.mailbox.deleted
                ]
                total = sum(len(raw) for _uidl, raw in active)
                self._send(f"+OK {len(active)} {total}".encode("ascii"))
            elif cmd == "UIDL":
                if not authenticated:
                    self._send(b"-ERR not authenticated")
                    continue
                self._send(b"+OK")
                for i, (uidl, _raw) in enumerate(self.server.mailbox.messages, 1):
                    if i in self.server.mailbox.deleted:
                        continue
                    self.wfile.write(f"{i} {uidl}\r\n".encode("ascii"))
                self.wfile.write(b".\r\n")
            elif cmd == "LIST":
                if not authenticated:
                    self._send(b"-ERR not authenticated")
                    continue
                self._send(b"+OK")
                for i, (_uidl, raw) in enumerate(self.server.mailbox.messages, 1):
                    if i in self.server.mailbox.deleted:
                        continue
                    self.wfile.write(f"{i} {len(raw)}\r\n".encode("ascii"))
                self.wfile.write(b".\r\n")
            elif cmd == "RETR":
                if not authenticated:
                    self._send(b"-ERR not authenticated")
                    continue
                num = int(arg)
                if (
                    num < 1
                    or num > len(self.server.mailbox.messages)
                    or num in self.server.mailbox.deleted
                ):
                    self._send(b"-ERR no such message")
                    continue
                _uidl, raw = self.server.mailbox.messages[num - 1]
                self._send(f"+OK {len(raw)} octets".encode("ascii"))
                self.wfile.write(raw)
                if not raw.endswith(b"\r\n"):
                    self.wfile.write(b"\r\n")
                self.wfile.write(b".\r\n")
            elif cmd == "DELE":
                if not authenticated:
                    self._send(b"-ERR not authenticated")
                    continue
                num = int(arg)
                self.server.mailbox.deleted.add(num)
                self._send(b"+OK deleted")
            elif cmd == "QUIT":
                self._send(b"+OK bye")
                break
            else:
                self._send(b"-ERR unknown command")

    def _send(self, data: bytes) -> None:
        self.wfile.write(data + b"\r\n")


class FakePop3Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, username: str, password: str) -> None:
        super().__init__(("127.0.0.1", 0), _Pop3Handler)
        self.username = username
        self.password = password
        self.mailbox = _Mailbox()
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.server_address[1]

    def add_message(self, uidl: str, raw: bytes) -> None:
        self.mailbox.add(uidl, raw)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
