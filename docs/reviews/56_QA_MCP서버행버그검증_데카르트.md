# MCP 서버 시작 hang 수정 검증 — 데카르트

## 판정: Go
무한대기실제로없어짐+신규회귀테스트가결함잡아냄. 정상기동재확인(지연·회귀없음). 출시막을결함없음.

## 검증결과
1. **AsgiHost.start() 동작**(asgi_host.py:157-185): asyncio.wait_for(self._started.wait(),timeout=10.0)가타임아웃시self._task.cancel()호출+태스크완료대기후TimeoutError재던짐. Python3.13(요구버전3.12+)이라wait_for가내장TimeoutError던져 except TimeoutError로정확히잡힘. 취소되면_serve()바깥finally가_close_socket()+_notify_exit()실행. 별도스크립트로수정본vs"cancel뺀변형"비교: 수정본은running=False+같은포트재바인딩가능, 변형은running=True+재바인딩시PortUnavailableError — 소켓이실제로풀림확인.
2. **신규회귀테스트**: lifespan의__aenter__가아무도set안하는Event를기다리게해멈춘lifespan을매번같은방식으로재현. TimeoutError발생/running=False/같은포트재바인딩 3가지확인. 단독15회반복15회모두통과(불안정성없음). 단실제session_manager.run()안(anyio task group)에서취소되는경로까지는안다룸(권고2관련).
3. **지정6파일: 44 passed(3.99초)**. Edison보고와일치.
4. **정상기동회귀+10초기준적절성**: 정상일땐wait_for바로끝나추가지연없음(실서버테스트최대1.25초). 10초구간은lifespan진입뿐(모듈import·frozen exe첫기동비용은구간밖). 실측0.4~1초대비10~20배여유 — 느린PC오탐위험낮음. 다만잘못실패하면자동재시도없어사용자가앱재시작필요(권고3).
5. **외부15초와내부10초관계**(app.py:2010행): 대체로안전. 외부15초가덮는구간: audit.purge_expired(상한없음)→소켓생성→host.start(10초+cancel후대기(상한없음)+시작확인루프최대2초). purge오래걸리면외부가먼저걸릴수있으나, submit()은결과대기만포기+내부코루틴은10초안에스스로정리하며상태내림 — 로그한줄더남는정도, 무한대기안생김. 주석의"내부가반드시먼저걸린다"표현만조금낙관적.
6. **이PC실제재확인**: 8765포트비어있음확인후`.venv`로python -m emailtomcp실행 — 앱시작(12:48:32.793)후약0.6초만에"MCP서버시작:127.0.0.1:8765"(12:48:33.419)찍힘. 인증없이POST시401, 헤더없으면415정상응답. 확인후프로세스종료+포트풀림확인.

## 발견사항·개선권고(출시비차단)
1. **[중] "같은포트재시도가능"은소켓차원에서만맞음**: MCP SDK의StreamableHTTPSessionManager.run()은인스턴스당한번만사용가능. lifespan진입전에_has_started=True가먼저설정돼 같은McpComponents로재시도하면RuntimeError. **Edison이후속제안한자동재시도/재시작버튼만들때는세션매니저(컴포넌트)를새로만들도록설계필요**.
2. **[하] cancel뒤의await self._task에상한없음**: 멈춘지점이취소를삼키거나막고있으면(anyio shield등) start()가다시무한대기빠질수있음 — 이대기도wait_for로상한(예:3초)걸것권장.
3. **[하] 10초타임아웃실패후자동복구안됨**: Edison후속과제제안과동일, 별도이슈등록권고.
4. **[참고]** 방화벽정책엔진경합원인은시점겹침정황추정이지확정아님. 다만이수정은원인과무관하게무한대기구조적결함을막아판단은안바뀜.
5. **[참고]** 작업트리에이번건과무관한변경(packaging/release/*, test_release_scripts.py, 54번리뷰문서)섞여있음 — 커밋시Newton에게나눠담도록요청권고.

## 관련 파일
`runtime/asgi_host.py`, `app.py`(1591-1627,1997-2012행), `tests/integration/test_mcp_http_binding.py`(130-157행)
