"""CLI 진입점.

`--version`/`--help`는 argparse가 처리하는 즉시(Qt·DB·네트워크를 초기화하기 전에) 출력하고
`SystemExit`으로 종료한다 — PyInstaller 빌드 스모크 테스트의 전제(갈릴레오 §0)다.
무거운 의존성(PySide6, storage, runtime)은 `main()`이 argparse를 통과한 뒤에만 import한다.
"""

from __future__ import annotations

import argparse
import os
import sys


def build_parser() -> argparse.ArgumentParser:
    from emailtomcp._version import __version__

    parser = argparse.ArgumentParser(
        prog="emailtomcp",
        description="EmailToMCP — 로컬 메일 클라이언트 + 내장 MCP 서버 (Claude 연동)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        default=None,
        help=(
            "앱 데이터 디렉터리 (기본: OS별 표준 경로). "
            "EMAILTOMCP_DATA_DIR 환경변수로도 설정할 수 있고, 이 옵션이 우선한다."
        ),
    )
    parser.add_argument(
        "--mcp-port",
        type=int,
        default=None,
        metavar="PORT",
        help=(
            "MCP 서버 포트(기본: 설정의 mcp.port, 처음엔 8765). 0(임의 포트)은 dev·테스트 "
            "빌드에서만 허용한다. 포트가 사용 중이면 다른 포트로 바꾸지 않고 MCP를 끈다."
        ),
    )
    parser.add_argument(
        "--mcp-stdio-proxy",
        action="store_true",
        help=(
            "Claude Code/Desktop용 stdio 프록시로 실행한다(창·DB 없이, 실행 중인 앱에 중계). "
            "토큰은 OS keyring에서 읽는다."
        ),
    )
    parser.add_argument(
        "--client",
        default="default",
        metavar="NAME",
        help="stdio 프록시가 쓸 토큰 프로필 이름(기본 default, --mcp-stdio-proxy와 함께 사용).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.data_dir:
        os.environ["EMAILTOMCP_DATA_DIR"] = args.data_dir

    if args.mcp_stdio_proxy:
        # 프록시 모드: QApplication·DB를 만들지 않는다(§6.4). keyring과 HTTP 중계만 쓴다.
        from emailtomcp.mcp_server.stdio_proxy import run_stdio_proxy
        from emailtomcp.secrets.keyring_store import KeyringSecretStore

        return run_stdio_proxy(secret_store=KeyringSecretStore(), client=args.client)

    # 여기서부터만 Qt/DB/네트워크를 건드리는 무거운 모듈을 import한다.
    from emailtomcp.app import run_app

    return run_app(mcp_port=args.mcp_port)


if __name__ == "__main__":
    sys.exit(main())
