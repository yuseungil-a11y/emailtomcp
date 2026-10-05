# P3 Phase A Low 항목 정리 — Edison

## 요약
D1, D2, L-1, L-4, L-5, L-6(선택이었으나 완전구현), D4, D5 모두 반영. 지정 테스트 204건 전부 통과(핵심4파일 119건 + P2회귀+I8+UI토글 포함 204건, 25초).

## 지시보다 넓게 바꾼 부분(확인됨, 타당)
1. **D1 범위 확장**: 지시는 "키없을때 켜기거부"였으나 세대**0**과 **끄기경로**도 함께 막음. 0도 같은 재설정경로(DB조작후 켜면 1부터 재시작). 키없는상태에서 끄면 기존코드가 0+1=1을 새로쓰는 우회 가능했음. `_read_generation`이 "1이상 정수"만 유효로 보고 키없음/0/음수/비정수/bool/JSON손상은 전부 None(손상) 반환하도록 수정. 켜기는 PolicyError거부, 끄기는 세대 새로안씀(§7.0 gate규칙 "없거나1미만이면disabled"와 동일기준). 대가: 이 상태가 되면 복구경로 없이 계속꺼진상태(fail-closed, DB파일 직접조작해야만 발생).
2. **L-1 유효값도 상향**: Spinoza 공식은 무효값에만 적용이었으나, 유효값도 기준(`max(1,MAX(enable_gen)+1)`)보다 작으면 올리도록 확장(컬럼이 이미 있던 개발DB에서 유효값이 기존잡세대보다 작으면 같은ABA위험). 값을 줄이는경우는 없음.
3. **L-5가 Phase B에 주는 영향**: m0003이 queue_state·autosend_state를 지우므로, **Phase B의 G8은 이 키가 없는 상태를 처리해야 함**("없으면 막는다"로 판정, 컨트롤러가 처음값 쓰기전까지 자동발송 안됨) — Phase B 지시서에 필수 반영.

## 항목별 수정
- **D1**: `storage/repositories/autoreply.py` 세대읽기/켜기/끄기 수정. 정상DB 최초켜기는 영향없음(m0003이 항상1이상 삽입, 1→2 정상). 회귀테스트3건(test_autoreply_toggle.py): 세대3→키삭제→expected0/1/3 켜기 전부거부, 키없을때 끄기가 세대 새로안씀, 새DB 첫켜기 1→2.
- **D2**: `test_architecture_autoreply_keys.py`에 'policy_auto_send' 문자열상수 검사 추가(구문분석, docstring/주석제외, 대소문자무시). 허용파일 4개(core/models.py, m0001_init.py, storage/transitions.py, storage/repositories/autoreply.py). 위반사례3종+docstring무시+허용파일존재 테스트 추가.
- **L-1**: `m0003_autoreply_toggle.py`에 `floor=max(1,COALESCE(MAX(enable_gen),0)+1)` 계산, 무효값→floor, 유효값→max(기존값,floor). 테스트: v2DB(기존잡세대7)에서 무효값6종→8, 유효값3→8, 유효값20→유지.
- **L-4**: `app.py`에서 키 정규화후 `autoreply.`접두사면 거부. 예외허용목록 `AUTOREPLY_GENERIC_WRITABLE_KEYS`(현재 비어있음) 신설. 기존 `AUTOREPLY_PROTECTED_SETTING_KEYS`는 아키텍처테스트 기준으로 유지. `autoreply_timeout_sec`(밑줄)은 접두사아니라 영향없음. 테스트: autoreply.*키6종(변형포함) 거부+DB불변, 허용목록동작 확인.
- **L-5**: m0003이 autoreply.enabled·queue_state·autosend_state 함께삭제. 아키텍처테스트의 "m0003이 쓸수있는키"에 2개 추가.
- **L-6**: `drain_autosend_outbox`에서 rule_id지정시 출처불명행(job_id NULL/잡삭제/잡의rule_id NULL)도 함께드레인(되돌리는방향이라 넓게잡아도 안전). 테스트: 다른규칙행 안건드림+잡의rule_id NULL케이스.
- **D4,D5**: DESIGN.md 970행("m0003이후 기본1"), 1211행(지시문구+L-1공식+D1+L-5 내용 보강), 보호키설명에 L-4접두사차단+허용목록 추가. autoreply.py docstring "(최초0→1)"→"(m0003이1로초기화, 이후첫켜기는1→2)".

## 테스트 결과
핵심4파일(test_autoreply_toggle, test_migrations, test_architecture_autoreply_keys, test_autoreply_ui_api): 119 passed. +P2회귀(test_mcp_tools, test_draft_service, test_send_service, test_transitions, test_settings)+test_drafts_i8_reset+UI토글: **204 passed**(25초). ruff/mypy 수정파일 통과, 기존 백로그(E501 등)는 무관.

## 변경 파일
`storage/repositories/autoreply.py`, `storage/migrations/m0003_autoreply_toggle.py`, `app.py`, `docs/DESIGN.md`, `tests/unit/{test_autoreply_toggle,test_migrations,test_architecture_autoreply_keys,test_autoreply_ui_api}.py`
