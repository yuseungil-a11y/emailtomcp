# EmailToMCP UI 디자인 가이드 (v1.0)

- 기준: 유티정보(주) 홈페이지(`https://utinfo.co.kr`) 실제 배포 CSS(`index-B-n2xNOH.css`)에서 추출한 디자인 토큰
- 추출 방법: 홈페이지 CSS를 직접 받아 `--프로퍼티: #값` 패턴과 사용 빈도를 분석함(2026-10-04). 홈페이지는 라이트/다크 모드를 모두 지원하는 토큰 체계(`:root`/`.dark`)를 쓰고 있어, 이 앱도 같은 구조(라이트 기본 + 다크 지원)로 맞춤.
- 전체 톤: **인디고 블루 계열**의 미니멀한 SaaS 스타일. 포인트 컬러 하나(#5678ff)를 버튼·강조 텍스트·포커스 표시에 일관되게 쓰고, 나머지는 무채색(그레이) 톤으로 차분하게 구성함.

## 1. 컬러 토큰

### 1.1 라이트 모드 (기본)

| 토큰 | 값 | 용도 |
|---|---|---|
| `background` | `#f7f8f9` | 앱 전체 배경 |
| `surface` | `#ffffff` | 카드/패널/그리드 배경 |
| `surface-muted` | `#f4f5f6` | 보조 패널(폴더트리, 상태줄) 배경 |
| `foreground-h1` | `#2c2d2e` | 제목, 발신자명(굵게) |
| `foreground-h2` | `#3c3d3e` | 부제목 |
| `foreground` | `#5b5b5d` | 본문 텍스트 |
| `foreground-muted` | `#717174` | 보조 텍스트(날짜, 크기, 미리보기 snippet) |
| `border` | `#d4dae6` | 구분선, 입력창 테두리 |
| `border-subtle` | `#e6e8ea` | 그리드 행 구분선 |
| `hover` | `#e1e6f0` | 리스트/그리드 호버 배경 |
| `primary` | `#5678ff` | 기본 강조색(버튼, 선택 항목, 포커스, 링크) |
| `primary-hover` | `#315aff` | 기본 버튼 호버(그라디언트 종점과 동일) |
| `primary-muted` | `#8a90fc` | 비활성 강조, 보조 배지 |
| `primary-gradient` | `linear-gradient(280deg, #5678ff, #315aff)` | 주요 CTA 버튼("보내기" 등), 헤더 액센트 바 |
| `destructive` | `#f15347` | 삭제·오류·긴급정지 |
| `destructive-hover` | `#d64338` | 삭제 버튼 호버 |
| `success` | `#248ff4` | 성공 상태(연결됨, 발송 완료) — 홈페이지 원본 토큰 그대로(그린이 아닌 블루 계열) |
| `favorite` | `#f7a443` | 중요 표시(별표/플래그) |
| `unread-dot` | `#5678ff` | 그리드의 안읽음 표시 점 |

### 1.2 다크 모드

| 토큰 | 값 |
|---|---|
| `background` | `#1f2021` |
| `surface` | `#1e2124` |
| `surface-muted` | `#2c2d2e` |
| `foreground-h1` | `#f1f3f7` |
| `foreground-h2` | `#f7f8f9` |
| `foreground` | `#d4dae6` |
| `foreground-muted` | `#adafb1` |
| `border` | `#68696e` |
| `border-subtle` | `#3c3d3e` |
| `hover` | `#3c3d3e` |
| `primary` / `primary-hover` / `primary-gradient` | 라이트와 동일(#5678ff → #315aff) |
| `destructive` | `#f15347` (호버 `#d64e38`) |
| `success` | `#248ff4` |
| `favorite` | `#f7a443` |

### 1.3 모서리 반경(원본 토큰 그대로)
`xs=2px` `sm=4px` `md=6px` `lg=8px` `xl=12px` `2xl=16px` — 버튼/입력창은 `md`, 카드·다이얼로그는 `lg`, 승인 다이얼로그 같은 강조 모달은 `xl` 권장.

## 2. 타이포그래피
- 기준 폰트: **Pretendard** (홈페이지와 동일). 시스템에 없을 수 있으므로 폴백 체인 필수.
  - Windows: `Pretendard, Malgun Gothic, Segoe UI, sans-serif`
  - macOS: `Pretendard, Apple SD Gothic Neo, -apple-system, sans-serif`
  - Qt 폰트 지정 시 리스트를 그대로 넘기면 OS가 설치된 첫 폰트를 고름. 번들에 Pretendard 폰트 파일을 포함하면(가변 폰트 OFL 라이선스) 모든 환경에서 동일하게 보임 — P1에서 결정.
- 크기: 제목 15–16px(semibold), 본문 13px(regular), 보조 텍스트 11–12px(regular), 그리드 헤더 12px(semibold, letter-spacing 살짝).

## 3. 적용 화면 매핑 (DESIGN.md §11 기준)
| 화면 요소 | 적용 토큰 |
|---|---|
| 메인 툴바 | `surface` 배경, 아이콘 `foreground`, 호버 `hover`, 포커스 링 `primary` |
| 폴더 트리 | `surface-muted` 배경, 선택 항목 `primary` 배경 10% 투명도 + `primary` 텍스트, 미읽음 숫자 배지는 `primary` 배경에 흰 텍스트 |
| 메일 그리드 | `surface` 배경, 짝수행 `surface-muted`(아주 옅게), 호버 `hover`, 선택행 `primary` 10% 틴트, 안읽음 행은 제목 `foreground-h1` + 안읽음 점(`unread-dot`), 읽음 행은 `foreground-muted` |
| 미리보기 패널 | `surface`, 발신자 `foreground-h1`(semibold), 날짜 `foreground-muted` |
| [보내기]/[회신] 등 1차 버튼 | `primary-gradient` 배경, 흰 텍스트, 호버 시 그림자 강조(홈페이지의 `box-shadow 0 5px 15px -3px #5678ff40` 재사용) |
| 보조 버튼(취소 등) | 투명 배경 + `border` 테두리 + `foreground` 텍스트 |
| [삭제]/[거부]/[긴급정지] | `destructive` 배경 또는 텍스트 |
| 승인 다이얼로그 | 외부 도메인 수신자는 `favorite`(주황) 배지로 강조, 승인 버튼은 지연 활성화 후 `primary-gradient` |
| 상태줄(메일 서버, MCP 서버, Claude 연결) | `surface-muted` 배경. 3단계 점 색상(QSS `statusDot` 속성, 2026-10-04 추가): 정상/연결됨=`success`(#248ff4), 연결중·대기=`primary-muted`(#8a90fc), 오류/끊김=`destructive`(#f15347). 메일 서버는 계정별로 폴더 트리에, 전체 집계는 상태줄에 표시한다 |
| 링크 | `primary`, 밑줄은 호버 시만 |

## 4. Qt 구현 지침
- PySide6는 CSS 변수를 지원하지 않으므로, 라이트/다크 QSS를 **각각 완성된 파일**로 두고 토큰 값을 직접 기록한다(`ui/resources/themes/light.qss`, `dark.qss` — 이번에 작성 완료).
- 버튼 종류 구분은 `widget.setProperty("variant", "primary"|"secondary"|"destructive")` 동적 프로퍼티 + QSS 속성 선택자(`QPushButton[variant="primary"]`)로 한다. objectName 남용보다 유지보수가 쉽다.
- 그라디언트 버튼은 QSS `background: qlineargradient(...)`로 구현(아래 theme.qss에 포함).
- 테마 전환은 `QApplication.setStyleSheet(theme_text)`로 전체 교체. OS 다크모드 감지는 Qt6의 `QStyleHints.colorScheme()`로 자동 감지 후 기본값 선택, 사용자가 설정에서 수동 전환 가능하게 한다(§11.7에 "테마: 시스템 설정 따름/라이트/다크" 옵션 추가 권고).
- 아이콘은 모노크롬 SVG(현재 `foreground` 또는 `primary` 색으로 틴트)를 권장 — 라이트/다크 전환 시 아이콘을 다시 그릴 필요 없이 QSS로 색만 바꿀 수 있음(`QIcon` 대신 SVG를 QPainter로 틴트하거나, 두 세트(라이트용/다크용) 리소스를 두는 방법 중 P1에서 선택).

## 5. 후속 작업
- 실제 Pretendard 폰트 파일 포함 여부는 P1에서 결정(라이선스는 OFL로 재배포 가능 확인됨 — 바이너리 용량만 고려).
- ~~로고/앱 아이콘은 유티정보 브랜드 로고를 그대로 쓰지 않고(사내 제품이 아닌 한 사내 자산 오용 소지), 동일 컬러 팔레트(#5678ff 계열)의 자체 이메일 아이콘을 새로 제작할 것을 권고.~~ → **완료(2026-10-04)**: 편지봉투+스파크(자동화) 모티프로 자체 제작. 소스 `packaging/icons/make_icon.py`(Pillow로 직접 그림, 외부 이미지 사용 없음), 산출물은 `src/emailtomcp/ui/resources/icons/`(app_icon.ico/.icns/.png)와 `packaging/icons/`에 있다.
