"""`emailtomcp`를 두 번(거의 동시에) 실행했을 때 하나만 남는지 확인한다(보안검토 P2 L-1).

L-1: "단일 인스턴스 파이프·소켓명이 사용자별 아님, listen() 결과 미확인" 지적 뒤, 코드상
결함(사용자별 구분 미흡, listen() 실패 미확인)은 확인되어 수정했다 — 단, 데카르트의 QA(#48)
포함 실제 PID 2개 동시 생존은 재현에 성공하지 못했다(원인은 사용자가 본 작업관리자의
`--mcp-stdio-proxy` 프로세스를 GUI와 별개로 오인했을 가능성이 유력, 둘 다 떠 있는 것은
정상 동작). 이 테스트는 그 결함이 재발하지 않는지 확인하는 회귀 테스트다.

이 테스트는 `python -m emailtomcp`(`__main__.main()` → `app.run_app()`)를 오프스크린
모드로 두 번 거의 동시에 띄워, 한쪽은 곧바로 종료되고(exit 0, "이미 실행 중인 인스턴스"
로그) 다른 한쪽만 계속 살아 있는지 확인한다. `tests/unit/test_version.py`의
`subprocess.run([sys.executable, "-m", "emailtomcp", ...])` 패턴을 그대로 따른다.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

STARTUP_TIMEOUT_SEC = 20.0
POLL_INTERVAL_SEC = 0.2
SURVIVAL_CHECK_SEC = 1.5


def _spawn(data_dir: Path) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["EMAILTOMCP_DATA_DIR"] = str(data_dir)
    env["QT_QPA_PLATFORM"] = "offscreen"
    # 로그(한글)를 부모가 utf-8로 그대로 디코딩할 수 있게 자식 stdout 인코딩을 고정한다
    # (Windows 콘솔 기본 cp949와 섞이면 문자열 비교가 깨진다).
    env["PYTHONIOENCODING"] = "utf-8"
    # 임의 포트(dev 빌드 전용, §4.6) — 이 PC에서 실제로 돌고 있는 다른 emailtomcp
    # 인스턴스(기본 포트 8765)와 충돌해 테스트가 들쭉날쭉해지는 것을 막는다.
    return subprocess.Popen(
        [sys.executable, "-m", "emailtomcp", "--mcp-port", "0"],
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )


def _terminate(*procs: subprocess.Popen[str]) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def test_second_launch_exits_immediately_and_only_one_instance_survives(
    tmp_path: Path,
) -> None:
    proc_a = _spawn(tmp_path)
    proc_b = _spawn(tmp_path)

    loser: subprocess.Popen[str] | None = None
    survivor: subprocess.Popen[str] | None = None
    deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
    try:
        while time.monotonic() < deadline:
            if proc_a.poll() is not None:
                loser, survivor = proc_a, proc_b
                break
            if proc_b.poll() is not None:
                loser, survivor = proc_b, proc_a
                break
            time.sleep(POLL_INTERVAL_SEC)

        assert loser is not None, (
            "두 프로세스 모두 제한 시간 안에 종료되지 않았다"
            "(둘 다 '첫 인스턴스'가 되어 떠 있을 가능성 — L-1 재발)"
        )
        assert loser.returncode == 0
        loser_output = loser.stdout.read() if loser.stdout else ""
        assert "이미 실행 중인 인스턴스" in loser_output, loser_output

        # 살아남은 쪽이 곧바로 같이 죽지 않는지(둘 다 "진 쪽"으로 오판되지 않았는지) 확인한다.
        time.sleep(SURVIVAL_CHECK_SEC)
        assert survivor is not None
        assert survivor.poll() is None, "살아남아야 할 인스턴스가 함께 종료되었다"
    finally:
        _terminate(proc_a, proc_b)
