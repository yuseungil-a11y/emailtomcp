"""IMAP 통합 테스트용 가짜 서버 (갈릴레오 §2 "fake POP3/IMAP 서버").

완벽한 RFC 3501 구현이 아니라, `ImapIncomingProvider`가 실제로 쓰는 최소 명령만
흉내낸다: LOGIN/CAPABILITY/LIST/SELECT/EXAMINE/UID SEARCH/UID FETCH/UID STORE/
UID COPY/UID MOVE(옵션)/EXPUNGE/UID EXPUNGE(옵션)/APPEND/LOGOUT.

`support_move`/`support_uidplus`로 서버 능력을 바꿔, DESIGN.md §2(b) N-01이 요구하는
"MOVE 지원 / UIDPLUS만 지원 / 둘 다 미지원" 세 구성을 테스트할 수 있다.
"""

from __future__ import annotations

import re
import shlex
import socketserver
import threading

_LITERAL_RE = re.compile(rb"\{(\d+)\+?\}\r?\n$")


def _read_command_parts(rfile, wfile) -> list[tuple[str, bytes]] | None:
    """명령 한 줄(들)을 읽는다. `{N}` 리터럴 선언을 만나면 "+ OK"로 계속 진행을
    알리고 N바이트를 그대로 읽어 ("literal", bytes)로 보존한다(APPEND 등에 필요).
    """
    parts: list[tuple[str, bytes]] = []
    while True:
        line = rfile.readline()
        if not line:
            return parts or None
        match = _LITERAL_RE.search(line)
        if match:
            parts.append(("text", line[: match.start()]))
            n = int(match.group(1))
            wfile.write(b"+ OK\r\n")
            wfile.flush()
            literal = b""
            while len(literal) < n:
                chunk = rfile.read(n - len(literal))
                if not chunk:
                    break
                literal += chunk
            parts.append(("literal", literal))
            continue
        parts.append(("text", line))
        break
    return parts


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _parse_set(spec: str, universe: list[int]) -> list[int]:
    spec = spec.strip()
    if not spec:
        return []
    result: list[int] = []
    last = universe[-1] if universe else 0
    for token in spec.split(","):
        if ":" in token:
            a, b = token.split(":", 1)
            lo = last if a == "*" else int(a)
            hi = last if b == "*" else int(b)
            lo, hi = min(lo, hi), max(lo, hi)
            result.extend(u for u in universe if lo <= u <= hi)
        else:
            n = last if token == "*" else int(token)
            if n in universe:
                result.append(n)
    return result


def _parse_flag_list(text: str) -> set[str]:
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    return {tok for tok in text.split() if tok}


class _Mailbox:
    def __init__(self, name: str, special_use: str | None = None) -> None:
        self.name = name
        self.special_use = special_use
        self.messages: dict[int, dict] = {}
        self.next_uid = 1

    def add(self, raw: bytes, flags: set[str] | None = None) -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.messages[uid] = {"flags": set(flags or ()), "raw": raw}
        return uid


class FakeImapServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        username: str,
        password: str,
        *,
        support_move: bool = True,
        support_uidplus: bool = True,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _ImapHandler)
        self.username = username
        self.password = password
        self.support_move = support_move
        self.support_uidplus = support_uidplus
        self.mailboxes: dict[str, _Mailbox] = {
            "INBOX": _Mailbox("INBOX"),
            "Trash": _Mailbox("Trash", special_use="\\Trash"),
            "Sent": _Mailbox("Sent", special_use="\\Sent"),
        }
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.server_address[1]

    def add_message(self, mailbox: str, raw: bytes, flags: set[str] | None = None) -> int:
        return self.mailboxes[mailbox].add(raw, flags)

    def capability_tokens(self) -> list[str]:
        caps = ["IMAP4rev1", "SPECIAL-USE"]
        if self.support_move:
            caps.append("MOVE")
        if self.support_uidplus:
            caps.append("UIDPLUS")
        return caps

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


class _ImapHandler(socketserver.StreamRequestHandler):
    server: FakeImapServer

    def setup(self) -> None:
        super().setup()
        self.authenticated = False
        self.selected: _Mailbox | None = None

    def handle(self) -> None:
        self.wfile.write(b"* OK FakeIMAP ready\r\n")
        while True:
            parts = _read_command_parts(self.rfile, self.wfile)
            if parts is None:
                break
            text = "".join(p[1].decode("latin-1") for p in parts if p[0] == "text")
            literals = [p[1] for p in parts if p[0] == "literal"]
            line = text.strip("\r\n")
            if not line:
                continue
            tag, _, remainder = line.partition(" ")
            remainder = remainder.strip()
            use_uid = False
            if remainder.upper().startswith("UID "):
                use_uid = True
                remainder = remainder[4:].strip()
            verb, _, args = remainder.partition(" ")
            verb = verb.upper()
            args = args.strip()
            try:
                closed = self._dispatch(tag, verb, args, use_uid, literals)
            except Exception as exc:  # noqa: BLE001 — fake 서버는 테스트 보조이므로 광범위 처리
                self._send(f"{tag} NO internal error: {exc}")
                closed = False
            if closed:
                break

    # --- 명령 처리 --------------------------------------------------------

    def _dispatch(
        self, tag: str, verb: str, args: str, use_uid: bool, literals: list[bytes]
    ) -> bool:
        if verb == "LOGIN":
            self._cmd_login(tag, args)
        elif verb == "CAPABILITY":
            self._cmd_capability(tag)
        elif verb in ("SELECT", "EXAMINE"):
            self._cmd_select(tag, args, readonly=(verb == "EXAMINE"))
        elif verb == "LIST":
            self._cmd_list(tag)
        elif verb == "SEARCH":
            self._cmd_search(tag)
        elif verb == "FETCH":
            self._cmd_fetch(tag, args)
        elif verb == "STORE":
            self._cmd_store(tag, args)
        elif verb == "COPY":
            self._cmd_copy(tag, args)
        elif verb == "MOVE":
            self._cmd_move(tag, args)
        elif verb == "EXPUNGE":
            self._cmd_expunge(tag, args, use_uid)
        elif verb == "APPEND":
            self._cmd_append(tag, args, literals)
        elif verb == "NOOP":
            self._send(f"{tag} OK NOOP completed")
        elif verb == "LOGOUT":
            self._send_untagged("BYE logging out")
            self._send(f"{tag} OK LOGOUT completed")
            return True
        else:
            self._send(f"{tag} BAD unknown command {verb}")
        return False

    def _cmd_login(self, tag: str, args: str) -> None:
        try:
            parts = shlex.split(args)
        except ValueError:
            parts = args.split()
        if len(parts) != 2 or parts[0] != self.server.username or parts[1] != self.server.password:
            self._send(f"{tag} NO LOGIN failed")
            return
        self.authenticated = True
        caps = " ".join(self.server.capability_tokens())
        self._send(f"{tag} OK [CAPABILITY {caps}] LOGIN completed")

    def _cmd_capability(self, tag: str) -> None:
        self._send_untagged("CAPABILITY " + " ".join(self.server.capability_tokens()))
        self._send(f"{tag} OK CAPABILITY completed")

    def _cmd_select(self, tag: str, args: str, *, readonly: bool) -> None:
        name = _unquote(args)
        mailbox = self.server.mailboxes.get(name)
        if mailbox is None:
            self._send(f"{tag} NO SELECT failed: no such mailbox")
            return
        self.selected = mailbox
        self._send_untagged(f"{len(mailbox.messages)} EXISTS")
        self._send_untagged("0 RECENT")
        self._send_untagged(r"FLAGS (\Seen \Flagged \Deleted \Answered \Draft)")
        self._send_untagged("OK [UIDVALIDITY 1] UIDs valid")
        self._send_untagged(f"OK [UIDNEXT {mailbox.next_uid}] Predicted next UID")
        mode = "READ-ONLY" if readonly else "READ-WRITE"
        verb = "EXAMINE" if readonly else "SELECT"
        self._send(f"{tag} OK [{mode}] {verb} completed")

    def _cmd_list(self, tag: str) -> None:
        for name, mbox in self.server.mailboxes.items():
            flags = r"\HasNoChildren"
            if mbox.special_use:
                flags += f" {mbox.special_use}"
            self._send_untagged(f'LIST ({flags}) "/" "{name}"')
        self._send(f"{tag} OK LIST completed")

    def _cmd_search(self, tag: str) -> None:
        mailbox = self._require_selected(tag)
        if mailbox is None:
            return
        uids = sorted(mailbox.messages.keys())
        self._send_untagged("SEARCH " + " ".join(str(u) for u in uids))
        self._send(f"{tag} OK SEARCH completed")

    def _cmd_fetch(self, tag: str, args: str) -> None:
        mailbox = self._require_selected(tag)
        if mailbox is None:
            return
        uidset_str, _, items_str = args.partition(" ")
        uids = _parse_set(uidset_str, sorted(mailbox.messages.keys()))
        items_str = items_str.strip()
        for uid in uids:
            msg = mailbox.messages.get(uid)
            if msg is None:
                continue
            flags = sorted(msg["flags"]) if "FLAGS" in items_str else None
            size = len(msg["raw"]) if "RFC822.SIZE" in items_str else None
            body = msg["raw"] if ("BODY" in items_str or "RFC822" in items_str) else None
            self._send_fetch_response(uid, flags=flags, size=size, body=body)
        self._send(f"{tag} OK FETCH completed")

    def _cmd_store(self, tag: str, args: str) -> None:
        mailbox = self._require_selected(tag)
        if mailbox is None:
            return
        uidset_str, _, rest = args.partition(" ")
        uids = _parse_set(uidset_str, sorted(mailbox.messages.keys()))
        op, _, flags_str = rest.strip().partition(" ")
        flags = _parse_flag_list(flags_str)
        for uid in uids:
            msg = mailbox.messages.get(uid)
            if msg is None:
                continue
            if op.startswith("+"):
                msg["flags"] |= flags
            elif op.startswith("-"):
                msg["flags"] -= flags
            else:
                msg["flags"] = set(flags)
            self._send_fetch_response(uid, flags=sorted(msg["flags"]))
        self._send(f"{tag} OK STORE completed")

    def _cmd_copy(self, tag: str, args: str) -> None:
        mailbox = self._require_selected(tag)
        if mailbox is None:
            return
        uidset_str, _, mailbox_name = args.partition(" ")
        dest = self.server.mailboxes.get(_unquote(mailbox_name))
        if dest is None:
            self._send(f"{tag} NO [TRYCREATE] mailbox doesn't exist")
            return
        uids = _parse_set(uidset_str, sorted(mailbox.messages.keys()))
        new_uids = []
        for uid in uids:
            msg = mailbox.messages.get(uid)
            if msg is None:
                continue
            new_uids.append(dest.add(msg["raw"], flags=set(msg["flags"])))
        if self.server.support_uidplus and new_uids:
            dst_set = ",".join(str(u) for u in new_uids)
            self._send(f"{tag} OK [COPYUID 1 {uidset_str} {dst_set}] COPY completed")
        else:
            self._send(f"{tag} OK COPY completed")

    def _cmd_move(self, tag: str, args: str) -> None:
        if not self.server.support_move:
            self._send(f"{tag} BAD unknown command MOVE")
            return
        mailbox = self._require_selected(tag)
        if mailbox is None:
            return
        uidset_str, _, mailbox_name = args.partition(" ")
        dest = self.server.mailboxes.get(_unquote(mailbox_name))
        if dest is None:
            self._send(f"{tag} NO [TRYCREATE] mailbox doesn't exist")
            return
        uids = _parse_set(uidset_str, sorted(mailbox.messages.keys()))
        new_uids = []
        for uid in uids:
            msg = mailbox.messages.pop(uid, None)
            if msg is None:
                continue
            new_uids.append(dest.add(msg["raw"], flags=set(msg["flags"])))
            self._send_untagged(f"{uid} EXPUNGE")
        if self.server.support_uidplus and new_uids:
            dst_set = ",".join(str(u) for u in new_uids)
            self._send(f"{tag} OK [COPYUID 1 {uidset_str} {dst_set}] MOVE completed")
        else:
            self._send(f"{tag} OK MOVE completed")

    def _cmd_expunge(self, tag: str, args: str, use_uid: bool) -> None:
        mailbox = self._require_selected(tag)
        if mailbox is None:
            return
        if use_uid:
            if not self.server.support_uidplus:
                self._send(f"{tag} BAD unknown command")
                return
            requested = set(_parse_set(args.strip(), sorted(mailbox.messages.keys())))
            target_uids = [
                uid
                for uid, msg in mailbox.messages.items()
                if "\\Deleted" in msg["flags"] and uid in requested
            ]
        else:
            target_uids = [
                uid for uid, msg in mailbox.messages.items() if "\\Deleted" in msg["flags"]
            ]
        for uid in sorted(target_uids):
            del mailbox.messages[uid]
            self._send_untagged(f"{uid} EXPUNGE")
        self._send(f"{tag} OK EXPUNGE completed")

    def _cmd_append(self, tag: str, args: str, literals: list[bytes]) -> None:
        mailbox_name, _, rest = args.partition(" ")
        mailbox = self.server.mailboxes.get(_unquote(mailbox_name))
        if mailbox is None:
            self._send(f"{tag} NO [TRYCREATE] mailbox doesn't exist")
            return
        flags = _parse_flag_list(rest.strip())
        body = literals[0] if literals else b""
        new_uid = mailbox.add(body, flags=flags)
        if self.server.support_uidplus:
            self._send(f"{tag} OK [APPENDUID 1 {new_uid}] APPEND completed")
        else:
            self._send(f"{tag} OK APPEND completed")

    # --- 응답 헬퍼 --------------------------------------------------------

    def _require_selected(self, tag: str) -> _Mailbox | None:
        if self.selected is None:
            self._send(f"{tag} NO no mailbox selected")
            return None
        return self.selected

    def _send(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode("latin-1"))

    def _send_untagged(self, line: str) -> None:
        self._send("* " + line)

    def _send_fetch_response(
        self,
        uid: int,
        *,
        flags: list[str] | None = None,
        size: int | None = None,
        body: bytes | None = None,
    ) -> None:
        segments = []
        if flags is not None:
            segments.append(f"FLAGS ({' '.join(flags)})")
        if size is not None:
            segments.append(f"RFC822.SIZE {size}")
        prefix = f"* {uid} FETCH (" + " ".join(segments)
        if body is not None:
            if segments:
                prefix += " "
            prefix += f"BODY[] {{{len(body)}}}\r\n"
            self.wfile.write(prefix.encode("latin-1"))
            self.wfile.write(body)
            self.wfile.write(b")\r\n")
        else:
            self.wfile.write((prefix + ")\r\n").encode("latin-1"))
