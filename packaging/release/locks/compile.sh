#!/usr/bin/env bash
# 릴리스 잠금파일(해시 고정) 재생성 — 레포 루트에서 실행: bash packaging/release/locks/compile.sh
# 보안검토 44 H-1: release.yml은 이 파일들을 `pip install --require-hashes --no-deps -r`로만 설치한다.
# 의존성(pyproject.toml / *.in)을 바꾸면 이 스크립트로 다시 만들고 diff를 검토한 뒤 커밋한다.
# CI 러너(Windows x64, macOS arm64, Ubuntu)가 모두 Python 3.12이므로 --universal --python-version 3.12.
set -euo pipefail
L=packaging/release/locks
COMMON=(--universal --python-version 3.12 --generate-hashes --quiet
        --custom-compile-command "bash packaging/release/locks/compile.sh")
uv pip compile pyproject.toml "$L/build-backend.in" --extra release "${COMMON[@]}" -o "$L/requirements-release.txt"
uv pip compile "$L/build-backend.in" "${COMMON[@]}" -o "$L/requirements-build-backend.txt"
uv pip compile "$L/manifest.in" "${COMMON[@]}" -o "$L/requirements-manifest.txt"
uv pip compile "$L/audit.in" "${COMMON[@]}" -o "$L/requirements-audit.txt"
