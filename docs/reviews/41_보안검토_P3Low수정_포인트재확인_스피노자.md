# P3 Low항목 수정(L-5, N-3, L-9) 포인트 재확인 — Spinoza

## 판정: Go
Critical/High/Medium없음. Low2건+Info1건. 지정3파일(test_autoreply_pipeline, test_autoreply_runner_parts, test_sync_service) 131 passed.

## 1. L-5(판정기G6재확인) — 이상없음
`rules/autosend_verify.py`: 초안생성경로는 commit_autoreply_result→verify→_commit_draft 하나뿐(우회경로없음). _submission_recorded와_g6_mismatch 둘다discard즉시반환 — 저장소(autoreply.py:1246)가discard받으면failed(security)+paused_security. 판정기예외/반환타입오류/바인딩어긋남/판정값불명 전부실패처리 — draft로통과하는경로없음. 세부조건정상(고정템플릿은허용도구빈집합, claude실행잡은exit_code None이어도0아니라폐기, tools_used는tuple타입이라오동작없음).
Info: PreflightResult는같은러너의같은parse_stream산출물이라 이재확인은독립증거가아니라러너회귀방지용2중방어. _submission_recorded도①번잡확인과거의겹침 — 둘다무해해그대로둬도됨.

## 2. N-3(abort와보안판정순서) — 이상없음, Low1건
`autoreply/job_runner.py:466-535`: 보안판정은프로세스종료+stdout확정후실행. fail_running_job은status=running일때만조건부갱신 — DB가먼저cancelled든아니든 결과는 failed(security)+보안정지 또는 pause_autosend_for_security로보안정지만, 둘중하나(보안신호소실안됨). 발동조건: pause_autosend_for_security는허용외도구있거나submit2회이상일때만호출+그상태서잡전이실패한경우만실행 — 신호없으면_security_failure가None반환, 정상종료/단순중지된잡은보안정지안됨(타임아웃경로도동일).

**Low N-3a(감사공백)**: 잡이이미cancelled인상태서보안정지만걸때 auto_reply_log에기록없이logger.error만남음 — 사용자가로그탭서보안정지이유를못찾음. 수정안: pause_autosend_for_security호출같은트랜잭션에서 _insert_log(outcome="security_signal",reason_code=reason,job_id=...,validation=...) 함께기록.

## 3. L-9(Message-ID재평가차단) — 설계충돌없음, Low1건
`storage/repositories/messages.py:147`, `mail/sync_service.py:286`: §7.9 M-A는설정변경시outbox→draft규칙이라L-9와무관. 판정방향"평가에서제외"(backfill과동일방향)라fail-safe원칙과일치. 차단범위를backfill사본까지넓힌것도§7.2"과거메일재평가금지"취지와일치(DESIGN G1행에반영됨).
Edison이밝힌잔여위험(같은Message-ID로자동회신억제)평가: **영향은가용성한정, 심각도Low**. 미리차단하려면공격자가정상메일Message-ID를그메일보다먼저알아내야함(일반MUA는무작위ID라사실상불가능, 예측가능ID쓰는발신시스템만이론상가능). 차단당해도메일은정상저장+사용자에게보임, PhaseB는자동발송없어초안만생성이라피해는"자동초안1건미생성"으로끝남(데이터유출·권한상승·오발송경로없음). **단PhaseC에서자동발송켜지면"중요자동응답누락"이실제피해될수있음**.

**Low L-9a**: 차단조회가계정+Message-ID만보고발신자는안봄. 수정안: has_autoreply_handled_copy에 from_addr_norm동일조건추가(공격자가From까지위조해야억제가능해지고, 그러면DMARC검증에걸릴여지생김). **Phase C착수전반영권고**.

Info: auto_reply_status는G2서비동기기록 — 같은Message-ID사본2통이G2평가전에연달아들어오면(메일링리스트+직접수신겹침등) 둘다평가될수있음. PhaseB는초안중복정도이고수신자레이트리밋·쿨다운이완화. **Phase C에서G8중복발송방지가이경우를덮는지확인권고**.

## 결론
L-5,N-3,L-9 모두의도대로동작+우회경로없음. Low2건(N-3a감사로그공백, L-9a발신자조건추가)은후속처리로충분. **Go**.
