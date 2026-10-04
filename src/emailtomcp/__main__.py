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
        default=0,
        metavar="PORT",
        help="MCP 서버 포트 (0이면 자동 선택). P2(MCP 서버)에서 실제로 쓰이며, 지금은 파싱만 한다.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.data_dir:
        os.environ["EMAILTOMCP_DATA_DIR"] = args.data_dir

    # 여기서부터만 Qt/DB/네트워크를 건드리는 무거운 모듈을 import한다.
    from emailtomcp.app import run_app

    return run_app(mcp_port=args.mcp_port)


if __name__ == "__main__":
    sys.exit(main())
