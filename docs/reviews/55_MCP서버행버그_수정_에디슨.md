# MCP 서버 시작 영구행(hang) 버그 수정 — Edison

## 원인(확정)
20초간격은우연. 실제원인: **AsgiHost.start()에자체타임아웃없어서** lifespan진입(MCP SDK StreamableHTTPSessionManager.run())이외부요인으로멈추면 backend.submit(mcp_runner.start(),timeout=15.0)의future.result(timeout=15.0)는TimeoutError내고포기하지만, 백엔드asyncio루프에제출된코루틴자체는취소안되고그대로계속돔(run_coroutine_threadsafe의일반동작 — submit()은결과기다리는쪽만포기, 작업은안멈춤). 이번건은그코루틴이**영원히**멈춰있었음.

### 재현·실측으로확인한사실
- 사용자보고프로세스(PID37680,12:23:14시작)가조사시점까지10분넘게생존, py-spy dump로보면백엔드스레드(emailtomcp-backend)는run_forever의select()에서idle(어느태스크도안돌고있음).
- netstat에서127.0.0.1:8765가LISTENING이었지만(소켓bind·listen은동기코드라이미끝남), curl http://127.0.0.1:8765/mcp는타임아웃(응답없음) — uvicorn이실제로serve()를시작못했다는뜻.
- AsgiHost._serve()코드: self._started.set()은**async with lifespan_cm: 진입성공한뒤**에야호출. session_manager.run()의__aenter__(lifespan진입)가멈추면_started이벤트가영원히set안되고 host.start()의await self._started.wait()가무기한블로킹. logger.info("MCP서버시작:...")가로그에전혀안찍힌것도이와일치.
- Windows이벤트로그(System): **하필그순간(12:23:03~04) 이PC에서"Claude"(Cowork)서비스가신규설치되며Windows Defender방화벽에다수의아웃바운드/인바운드규칙이동시추가(이벤트ID2097)** — 방화벽정책엔진(BFE/mpssvc)이그순간바쁘게규칙을커밋하던타이밍이 lifespan진입경로의네트워크스택관련호출과경합해비정상적으로지연/정지된것으로보임. Defender실시간보호자체는꺼져있어(AntivirusEnabled/RealTimeProtectionEnabled:False) 바이러스스캔은원인아님.
- 재현은간헐적: dev venv와실제설치된frozen exe를각각5회이상반복실행했으나모두0.4~1초내정상기동. 즉코드버그는"항상걸리는레이스"가아니라, **외부요인(방화벽정책엔진경합등)으로lifespan진입이멈추면복구불가능한영구행에빠진다**는구조적결함. 증거(소켓은열렸는데uvicorn이안돌고,10분넘게복구안됨,재시작수단전혀없음)로볼때일회성우연이아니라**재발가능한설계결함**.
- mcp_runner.start()는앱기동시딱한번만호출, UI에"MCP재시작"수단전혀없어 한번걸리면**앱완전재시작(프로세스kill)안하는한영원히복구불가능**했음.

## 수정내용
1. **runtime/asgi_host.py**: AsgiHost.start()에 timeout(기본10초, DEFAULT_START_TIMEOUT_SECONDS)파라미터추가. self._started.wait()를 asyncio.wait_for(...,timeout=timeout)로감싸고, 타임아웃시 self._task.cancel()후완료를기다린다음(_serve()의기존try/finally가소켓정리) 명확한TimeoutError를던지도록변경. 이제lifespan진입이멈춰도**10초안에반드시실패로끝나고소켓이닫혀**, 같은포트로재시도가능.
2. **app.py**: McpServiceRunner.start()는기존처럼await host.start()호출(기본타임아웃자동적용)이라변경없이새보호를받음(기존except Exception이TimeoutError도이미포괄해McpStatusChanged(error=...)로상태내리는기존경로그대로). run_app()의backend.submit(mcp_runner.start(),timeout=15.0)위에, 내부10초타임아웃과의관계및과거버그설명주석추가(동작변경없음, 외부15초타임아웃값유지 — 내부10초가먼저걸려충분한여유).

## 테스트
신규회귀테스트: test_mcp_http_binding.py::test_start_timeout_cancels_task_and_frees_socket_for_retry — lifespan __aenter__가영원히멈추는가짜lifespan으로host.start(timeout=0.2)호출해 TimeoutError나는지, host.running이False인지, **같은포트로즉시재바인딩성공**하는지(소켓실제해제됐는지)확인.

지정실행: test_mcp_http_binding.py+test_mcp_runner.py+test_backend.py+test_backend_periodic.py+test_architecture_no_qt_in_backend.py+test_asgi_guard.py → **44 passed**. ruff/mypy(asgi_host.py)통과(app.py기존mypy오류3건은707/710행무관한기존이슈, git diff로확인).

## 실측확인(이PC직접)
사용자보고PID(37680)는py-spy dump+curl타임아웃으로"영구행"상태확인후종료함(조사목적상필요, **사용자작업중이었다면다시실행필요** — 확인차정상종료). 수정적용후 `.venv/Scripts/python.exe -m emailtomcp`로실제기동 — 로그에타임아웃없이"MCP서버시작:127.0.0.1:8765"가즉시(약0.5초)찍힘, curl -X POST http://127.0.0.1:8765/mcp가(인증없는요청)401 Unauthorized를**정상응답**(이전행상태와대조 — uvicorn이실제로요청처리중이라는뜻).

## 후속개선제안(이번범위밖)
mcp_runner.start()는앱기동시1회만호출+UI에"MCP재시작"수단없음. 이번수정으로"영원히행"은막았지만, 10초타임아웃실패후엔여전히수동재시작필요. housekeeping주기작업에"꺼져있으면자동재시도"로직추가또는설정/상태패널에수동재시작버튼추가후속작업제안.

## 변경 파일
`runtime/asgi_host.py`(핵심수정), `app.py`(주석추가,동작변경없음), `tests/integration/test_mcp_http_binding.py`(회귀테스트추가)

커밋 안함.
