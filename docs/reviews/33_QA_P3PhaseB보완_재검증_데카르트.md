# P3 Phase B 보완 재검증 — 데카르트

## 결론
- **코드수준: 조건부Go.** Critical·High없음. 지난지적(M-1,M-3,M-4,L-4) 전부 의도대로고쳐짐. 단 M-1고친방식때문에 새결함1건(M-5,Medium)+Low3건 발생.
- **출시게이트(v1.2.0): No-Go(보류)유지**. 이유: ①R1실측미수행(docs/research/엔 00_기술조사.md만있고 01_R1_실측.md없음, 승인절차미진행) ②M-5출시전수정권고.
- 고정템플릿경로는R1무관완결. "Claude작성"경로는실제claude로한번도안돌려본상태그대로.

## 1. 지정테스트재현
11개파일: **214 passed(15.6초)**. Edison보고 파일별건수와정확히일치.

## 2. 중점검증

**①M-1(backfill오판) 수정확인**: 판정이제 `folders.initial_sync_done`만봄(sync_service.py:205). 실제이동경로(move_to_folder, remote_uid유지)로재현 — 최초2통은잡0건(backfill정상), 받은편지함비운뒤새메일1통은queued+잡1건생성(더이상backfill아님). 업그레이드초기값판정은대체로타당하나빈틈있음(L-7).

**②M-3(작성창경고배너) 수정확인**: G7거친초안서 get_draft_guard_findings가{url,money}반환+배너평문표시(UI테스트3건+잡탭1건확인). 사용자직접작성/MCP[수정후발송]/새메일엔배너안뜸. 가드기록없으면guard_unknown으로오히려경고(실패해도숨기지않는방향,타당).

**[초안열기]추가(범위조정1)는 꼭필요했던 최소추가로판단**: 이버튼없으면 자동회신초안(status='draft')열UI가 아예없음(가상폴더는보낼편지함/승인대기둘뿐, open_existing호출처는[수정후발송]하나뿐) — 없으면M-3배너를볼방법이없는기능이됨.

**신규경로안전성: 문제없음**: 단일사용자데스크톱앱이라"다른사용자"개념없음. 초안은draft.account_id로열리고잡의초안은G7이메시지계정으로생성 — 계정섞임없음. status!='draft'면안열림. discard_on_cancel=False라[취소]눌러도초안안지워짐. 발송은사람이작성창서직접(기존경로), 자동발송경로는안늘어남.

**③M-4(신뢰AR입력란) 수정확인**: 두입력란 TRUST_FIELDS_EDITABLE=False로비활성(account_dialog.py:392-398). _collect_fields에서두키빠져 저장해도기존DB값불변. 기존값은표시만. 백엔드update_account는여전히두키받지만PhaseB엔영향없음.

**④출력가드금액정규식(범위조정2): 놓치던금액은이제잡지만 오탐늘어남→L-8**
이제잡는것: "1,500,000원을","100만원입니다","50달러를","5천원","100usd"
오탐사례(직접확인): **"1.원인분석","2.원본파일","3.원활한"**(번호목록뒤'원', 업무메일흔함-가장중요), "제3원칙","4원소","2원화","회의실3원격","버전2.0원문","2억년","3억제"
원인: `\d[\d,.]*\s?(?:…|원|억|…)`가'.'과공백삼킨뒤'원'시작단어전체를금액으로봄.
오탐없었던것: "10월3일원격회의","회원10명". 놓치는것(이번수정전부터있던미탐): "100USDT"(가상화폐피싱관련),"오십만원"(한글숫자).

**⑤L-4(revalidate_current)순서: 확인**: 순서는 DB취소커밋(cancel_running_job, running일때만CAS)→abort_jobs(먼저토큰폐기→kill_tree). 테스트가revoke인덱스가kill인덱스보다앞인지직접단언(규칙삭제/중지,계정비활성3종). 경합처리: abort_jobs는_current_job in ids일때만kill — 그사이다음잡으로넘어갔으면새잡은안죽임. 호출위치: 규칙저장(수정)·삭제·중지, 계정auto_reply_enabled/enabled변경커밋직후. 이호출실패해도G7gate가결과막음.

## 3. 새로발견한결함
| # | 심각도 | 위치 | 내용 | 권고 |
|---|---|---|---|---|
| **M-5** | Medium(기능,M-1수정으로생긴문제) | sync_service.py:232 | 최초동기화중**계속실패하는메일1통**만있어도(saved!=len(summaries)) 완료표식이영원히안찍힘 → 그폴더이후새메일전부·계속backfill로조용히빠짐. **재현**: uid"bad"fetch항상예외→3회동기화→새메일is_backfill=True. 예전로직은1통만저장돼도그다음부턴정상이었어이경우퇴행. 매뉴얼04:104-105"다음동기화까지는"도실제와다름 | 최초동기화목록의UID집합(또는IMAP최대UID)기록, 그목록에있던메일만backfill처리후표식은바로찍기. 또는시도N회후강제완료+로그·UI표시 |
| L-7 | Low(업그레이드1회성) | m0004_folder_initial_sync.py:47-51 | 판정이폴더단위라 **업그레이드전에이미받은편지함비운사용자**는INBOX가0으로남음(실이동경로는remote_uid유지한채folder만바꿔 흔적이휴지통쪽에만남음). **재현**: m0004후INBOX=0,휴지통=1 — 업그레이드후첫동기화새메일이한번조용히backfill로빠짐(M-1증상1회재발) | INBOX판정을계정단위흔적으로: 그계정어느폴더든remote_uid메일있거나 original_folder_id가INBOX인메일있으면1 |
| L-8 | Low(PhaseB는배너소음뿐,PhaseC는불필요한강등) | output_guard.py:62 | 위④오탐("1.원인"등) | 숫자끝을 `\d(?:[\d,]*\d)?(?:\.\d+)?`로제한, 단위뒤 '원인/원본/원문/원활/원칙/원소/원격/원하' 등단어오면제외하는부정전방탐색추가. 판정로직이라Spinoza확인필요. USDT미탐은PhaseC전별도검토 |
| L-9 | Low(관찰) | sync_service+app._reflect_imap_move | 서버이동실패(오프라인등)상태서로컬만휴지통이동한메일이 다음동기화때INBOX재수신+이제backfill아니라서재평가됨(예전엔받은편지함완전히빈경우만우연히backfill로막힘). 결과물초안뿐+72시간제한있어영향작음. L-4(UIDVALIDITY미추적)와같은부류로함께처리 | PhaseC전Message-ID기준재평가차단 |
| I-4 | Info | compose_window.py:255 | 고정템플릿초안에도"Claude가만든초안"문구(MCP[수정후발송]과문구공유) | 문구일반화("자동회신/Claude초안") |

## 4. 우선순위
1.M-5수정+매뉴얼04:104-105정정 2.R1실측·승인(출시게이트) 3.L-8정규식보정(Spinoza확인) 4.L-7 m0004판정보완 5.L-9,I-4

## 관련 파일
`mail/sync_service.py`, `storage/migrations/m0004_folder_initial_sync.py`, `rules/output_guard.py`, `ui/dialogs/{mcp_panel,compose_window,account_dialog}.py`, `autoreply/job_runner.py`, `app.py`, `docs/manual/04_규칙설정.md`
