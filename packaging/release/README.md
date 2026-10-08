# 릴리스 절차 (빌드 → 오프라인 서명 → Pages 반영)

근거: `docs/DESIGN.md` §14.3(Velopack), §14.4(매니페스트 스펙), §14.7(서명키 운영), §15.4(release.yml), §15.6(릴리스 체크리스트).

## 역할 분리 (반드시 지킬 것)

| 단계 | 어디서 | 누가 | 비밀키 |
|---|---|---|---|
| 빌드, SHA256, SBOM, provenance, **서명 전 후보 매니페스트**, draft 릴리스 업로드 | GitHub Actions(`.github/workflows/release.yml`) | 자동(draft 생성은 Environment `release` 승인 후) | **없음** |
| 빌드 증명(`gh attestation verify`) 필수 확인 + 산출물 sha256 재대조 + `stable.json` 서명 | 메인테이너 PC(`sign_release.py`) | 메인테이너 | 오프라인 USB |
| draft → release publish | GitHub 웹/`gh` | 메인테이너 | 없음 |
| 서명된 매니페스트를 Pages `manifest/`에 커밋·push | git | **Newton(vcs-manager)** | 없음 |

- 서명 비밀키는 **GitHub Secrets, CI, 온라인 PC 어디에도 두지 않는다**(2026-10-05 결정 D-8, §14.7-2).
- 이 폴더의 스크립트는 git 커밋/push를 하지 않는다. git 작업은 Newton에게 요청한다.

## 파일

| 파일 | 실행 위치 | 역할 |
|---|---|---|
| `release_common.py` | (공용 모듈) | sha256, 직렬화(키 정렬·LF·UTF-8), 앱 스키마로 자체검증, 산출물 분류 |
| `build_velopack.py` | CI(Windows) | PyInstaller onedir(`dist/emailtomcp/`) → `vpk pack` |
| `build_manifest.py` | CI | 산출물 해시로 `manifest-candidate.json` 생성(서명 안 함) |
| `sign_release.py` | **메인테이너 PC만** | K1 고정값 확인 → 시계 확인 → 버전 3중 일치 → 빌드 증명 확인 → 산출물 재대조 → 게시본 대비 역행 검사 → `stable.json` 생성 → `minisign` 서명 → 앱 검증기로 자체검증. `--resign`으로 재서명 |
| `locks/*.in`, `locks/requirements-*.txt` | (잠금파일) | 릴리스 빌드 의존성 해시 고정(보안검토 44 H-1). `locks/compile.sh`로 재생성 |

스크립트는 앱 코드(`emailtomcp.update.manifest`/`keys`/`verifier`/`version`)를 그대로 import해 쓴다.
그래서 **레포 루트에서 `pip install -e .`을 한 환경**에서 `python packaging/release/<스크립트>.py`로 실행한다
(`packaging/`은 PyPI `packaging`과 이름이 겹쳐 파이썬 패키지로 import하지 않는다).

---

## 0. 사전 준비 (1회)

### 메인테이너 PC (서명용)
1. 이 저장소를 체크아웃하고 가상환경에서 `pip install -e .`
2. **minisign 설치**
   - Windows: `scoop install minisign` 또는 <https://github.com/jedisct1/minisign/releases>의 zip을 풀어 PATH에 두기
   - macOS: `brew install minisign`
   - PATH에 넣기 싫으면 서명할 때 `--minisign <minisign.exe 경로>`로 지정
3. 비밀키(K1, key_id `205BD649DF53346C`)는 암호화 USB에만 둔다. **저장소 폴더 안으로 복사하지 않는다**
   (`sign_release.py`는 비밀키 경로의 상위 폴더 어디에든 `.git`이 있으면 — 이 저장소, 다른 clone,
   Pages 소스 체크아웃 등 — 실행을 거부한다. `.gitignore`에도 `*.key`가 있다).
4. **(필수) GitHub CLI `gh` + `gh auth login`** — `sign_release.py`가 서명 전에 `gh attestation verify`(빌드 증명)와
   `gh release list`(재서명 기준점)를 반드시 실행한다. 없으면 서명하지 않는다. PATH에 없으면 `--gh <gh.exe 경로>`.
5. 서명 PC는 **네트워크에 연결**돼 있어야 한다(빌드 증명 조회, github.com `Date` 헤더로 시계 확인, 현재 Pages
   게시본 조회). 오프라인이어야 하는 것은 비밀키(USB)뿐이다. PC 시계가 서버와 5분 넘게 어긋나면 중단한다.

### 저장소 설정 (1회, 관리자)
- GitHub Pages 활성화. 매니페스트 게시 주소는 `https://yuseungil-a11y.github.io/emailtomcp/manifest/`
  (`update/release_client.py`의 `DEFAULT_BASE_URL`). Pages 소스(예: `gh-pages` 브랜치 루트, 또는 main의 `/docs`)
  아래 `manifest/` 폴더가 이 주소에 대응한다 — 어느 쪽인지 확정되면 이 문서에 적어 둔다.
- 아래 세 가지는 **코드로 할 수 없고 저장소 관리자가 GitHub 웹에서 직접 설정해야 한다**(보안검토 44 M-1).
  이 설정이 없으면 워크플로의 보호 장치 일부가 실제로 동작하지 않는다.

#### (1) GitHub Environment `release` + 승인자 — **필수**
`release.yml`의 `draft-release` job(유일하게 `contents: write`를 가진 job)은 `environment: release`를 참조한다.
**실제 Environment를 만들고 승인자를 지정해야 승인 게이트가 동작한다** — Environment가 없으면 GitHub가 첫 실행 때
보호 규칙 없는 빈 Environment를 자동으로 만들어 승인 없이 통과시킨다.
- Settings → Environments → **New environment** → 이름 `release`
- **Required reviewers**: 메인테이너(본인) 지정. 가능하면 **Prevent self-review**는 상황에 맞게(1인 운영이면 끔)
- **Deployment branches and tags**: *Selected branches and tags* → **Tag** 규칙 `v*` 추가(태그 외 ref에서 이 Environment 사용 금지)
- Environment secrets: **넣지 않는다**(서명키는 절대 여기 두지 않음, D-8)

#### (2) `v*` 태그 보호 ruleset — **rc 시험 태그 전에도 필수**
태그를 push할 수 있으면 그 태그 시점의 워크플로 내용을 임의로 바꿀 수 있으므로 태그 생성·이동·삭제를 막는다.
- Settings → Rules → **Rulesets** → **New ruleset** → **New tag ruleset**
- Ruleset name: `release-tags`, Enforcement status: **Active**
- Bypass list: 비워 두거나 Repository admin(메인테이너)만. **GitHub Actions/앱은 넣지 않는다**
- Target tags: **Include by pattern** → `v*`
- Rules(체크):
  - **Restrict creations** — bypass 대상(메인테이너)만 `v*` 태그 생성 가능
  - **Restrict updates** — 태그 이동(재지정) 금지
  - **Restrict deletions** — 태그 삭제 금지
  - **Block force pushes** — 강제 push 금지
- `gh` CLI로 하려면(관리자 권한으로 로그인된 상태에서):
  ```
  gh api -X POST repos/yuseungil-a11y/emailtomcp/rulesets --input ruleset-tags.json
  ```
  `ruleset-tags.json`:
  ```json
  {
    "name": "release-tags",
    "target": "tag",
    "enforcement": "active",
    "conditions": { "ref_name": { "include": ["refs/tags/v*"], "exclude": [] } },
    "rules": [
      { "type": "creation" },
      { "type": "update" },
      { "type": "deletion" },
      { "type": "non_fast_forward" }
    ],
    "bypass_actors": [
      { "actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always" }
    ]
  }
  ```
  (`actor_id: 5` = Repository admin 역할. 2026-10-05 작성 시점에 이 PC의 `gh`가 로그인돼 있지 않아 실제 적용은 못 했다 —
  위 웹 UI 절차나 이 명령으로 관리자가 직접 적용할 것.)

#### (3) main · Pages 소스 브랜치 보호
- `main`(및 Pages 소스가 `gh-pages`라면 그 브랜치)에 branch ruleset: **Restrict deletions**, **Block force pushes**,
  **Require a pull request before merging**(1인 운영이면 승인 수 0이라도 PR 경유). Bypass list에 GitHub Actions를 넣지 않는다
  → `draft-release` job의 `GITHUB_TOKEN`(contents: write)으로 이 브랜치들을 직접 push할 수 없게 된다.
- Settings → Actions → General → Workflow permissions: **Read repository contents and packages permissions**(기본 읽기 전용),
  "Allow GitHub Actions to create and approve pull requests"는 끔.

### Velopack CLI (CI가 자동 설치, 로컬 빌드 시에만 필요)
```
dotnet tool install -g vpk --version 1.2.161
```
- CI는 `dotnet tool install`에 해시 고정 옵션이 없어서, nupkg를 직접 받아 `release.yml`의 `VPK_SHA256`과 대조한 뒤
  nuget.org를 뺀 로컬 피드에서만 설치하고, 설치 후 `dotnet tool list -g`와 `vpk -h` 첫 줄(`Velopack CLI 1.2.161`)을
  로그에 남긴다(vpk 1.2.161에는 `--version` 옵션이 없음). vpk 버전을 올리면 `VPK_VERSION`과 `VPK_SHA256`을 함께 바꾼다
  (sha256은 nuget.org 카탈로그의 packageHash(SHA512)와 대조해 확인한 파일에서 계산).

### 릴리스 의존성 잠금파일(해시 고정, 보안검토 44 H-1)
CI는 파이썬 패키지를 **`pip install --require-hashes --no-deps -r packaging/release/locks/requirements-*.txt`로만** 설치하고,
앱 자체는 `pip install --no-deps --no-build-isolation -e .`로 올린다(빌드 백엔드 hatchling/hatch-vcs/editables도 잠금파일에서).
`pip install --upgrade pip` 같은 고정 안 된 설치는 하지 않는다.

| 잠금파일 | 입력 | 쓰는 job |
|---|---|---|
| `requirements-release.txt` | `pyproject.toml`(본 의존성 + `release` extra) + `build-backend.in` | `build` |
| `requirements-build-backend.txt` | `build-backend.in` | `validate` |
| `requirements-manifest.txt` | `manifest.in`(pydantic/packaging/cryptography + 빌드 백엔드) | `prepare-release` |
| `requirements-audit.txt` | `audit.in`(pip-audit) | `audit`, `build`(별도 venv) |

- 의존성(pyproject.toml 또는 `*.in`)을 바꾸면 레포 루트에서 `bash packaging/release/locks/compile.sh`(uv 필요)로 다시 만들고,
  diff를 검토한 뒤 커밋한다(Newton). CI 러너가 Windows/macOS/Ubuntu라 `--universal --python-version 3.12`로 만든다.
- `audit` job이 네 잠금파일 전부를, `build` job이 실제 설치된 빌드 환경을 `pip-audit`로 검사한다. 알려진 취약점이 있으면 빌드가 멈춘다.
- 파이썬 훅 패키지 `velopack`도 같은 버전(1.2.161)이다: `pip install -e ".[release]"`
  (`pyproject.toml`의 `release` extra — PyInstaller 6.22.3과 velopack 1.2.161 고정).
- 로컬에서 명령만 확인: `python packaging/release/build_velopack.py --version 1.0.1 --dry-run`

---

## 1. 릴리스 전 확인

- `docs/DESIGN.md` §15.6 체크리스트(A~E)를 진행한다. 데카르트 Go 판정 후 태그를 만든다.
- 이번 릴리스의 `min_version_floor`를 정한다(기본 `1.0.0`). 올려야 하면 release.yml의
  `build_manifest.py` 호출에 `--min-floor X.Y.Z`를, 보안 릴리스면 `--security`를, 요약이 필요하면
  `--notes "..."`를 추가한다(워크플로 수정은 커밋이 필요하므로 태그 전에 Newton에게 요청).

## 2. 태그 생성 → push (Newton에게 요청)

- annotated 태그 `vX.Y.Z`(예: `v1.0.1`)를 **릴리스할 커밋**에 만들고 push한다. 태그는 불변이다(재사용·이동 금지, §15.2).
- 허용 형식: `vX.Y.Z` 또는 `vX.Y.ZrcN`. `v1.1.0.dev0` 같은 개발 기준 태그는 워크플로가 즉시 종료(성공)하고 아무것도 만들지 않는다.

## 3. GitHub Actions 확인

저장소 → Actions → **Release** 워크플로에서 5개 job을 확인한다.

| job | 권한 | 하는 일 | 실패하면 |
|---|---|---|---|
| `validate` | 읽기 | 태그 정규식, 작업트리 clean, 잠금파일의 빌드 백엔드로 `pip install --no-deps --no-build-isolation -e .` → `_version.py` == 태그(`+`/`.dev` 없음) | 태그가 잘못됐거나 태그 커밋이 아님 → 새 버전 번호로 다시(태그 이동 금지) |
| `audit` | 읽기 | 잠금파일 4개를 `pip-audit`로 검사 | 취약 버전을 올린 잠금파일로 갱신(`locks/compile.sh`) 후 새 태그 |
| `build` (windows-latest, macos-latest) | 읽기 + id-token/attestations | 해시 고정 설치 → `pip check` → 설치 환경 `pip-audit` → PyInstaller → `--version` 스모크 → (Windows) sha256 대조한 vpk로 `vpk pack` / (macOS) onedir zip → SBOM(`pip list` JSON) → **산출물 provenance(빌드 증명)** | 로그 확인 후 수정, 새 태그로 다시 |
| `prepare-release` | 읽기 + id-token/attestations | `manifest-candidate.json` 생성(rc 태그는 생략), 자산 평탄화 + `SHA256SUMS.txt`, 이 두 파일의 provenance → artifact `release-upload` | 로그 확인 |
| `draft-release` | **contents: write**, Environment `release`(승인 필요) | checkout·pip 없이 `release-upload` artifact를 그대로 **draft 릴리스**로 생성 | 같은 태그의 릴리스(draft 포함)가 이미 있으면 **실패**한다(덮어쓰지 않음, L-4) — 기존 draft를 확인·삭제한 뒤 워크플로를 재실행 |

`draft-release`는 Environment `release`의 승인자가 Actions 화면에서 **Approve**해야 실행된다(0단계 "저장소 설정" (1)).
워크플로는 **서명도 publish도 하지 않는다.** 끝나면 Releases에 draft가 하나 생긴다.

## 4. draft 자산 내려받기

```
gh release download v1.0.1 --repo yuseungil-a11y/emailtomcp --dir D:\release\v1.0.1
```
(웹에서 draft를 열어 자산을 모두 받아 한 폴더에 두어도 된다.) 폴더에 `manifest-candidate.json`,
`EmailToMCP-...-Setup.exe`, `...-full.nupkg`, `releases.stable.json`, `EmailToMCP-1.0.1-macos-arm64.zip`,
`SHA256SUMS.txt`, `sbom-pip-*.json` 등이 있어야 한다.

빌드 증명 확인은 **더 이상 선택이 아니다** — 5단계 `sign_release.py`가 후보 매니페스트와 매니페스트의 모든 산출물에 대해
아래 명령을 자동으로 실행하고, 하나라도 실패하면 서명하지 않는다(보안검토 44 H-2). 수동으로 미리 보고 싶으면:
```
gh attestation verify D:\release\v1.0.1\EmailToMCP-stable-Setup.exe --repo yuseungil-a11y/emailtomcp ^
    --signer-workflow yuseungil-a11y/emailtomcp/.github/workflows/release.yml ^
    --source-ref refs/tags/v1.0.1 --deny-self-hosted-runners
```

## 5. 오프라인 서명 (메인테이너 PC, 비밀키 USB 연결)

레포 루트에서:
```
python packaging/release/sign_release.py ^
    --candidate D:\release\v1.0.1\manifest-candidate.json ^
    --artifacts-dir D:\release\v1.0.1 ^
    --expect-version 1.0.1 ^
    --key E:\minisign\emailtomcp-K1.key ^
    --out-dir release-out
```
(macOS/리눅스 셸은 줄 끝 `^` 대신 `\`.) `--expect-version`은 **필수**이며, 후보 파일을 보고 복사하지 말고
릴리스하려는 버전을 직접 입력한다.

스크립트가 하는 일(하나라도 실패하면 중단하고 결과 파일을 남기지 않는다):
1. 앱 내장 공개키 목록(`update/keys.py`)이 스크립트에 고정한 **K1(key_id `205BD649DF53346C`, 공개키 값까지) 하나와 정확히 일치**하는지
   확인한다. 키가 없거나, 다른 키가 섞였거나, 공개키 값이 다르면 중단(보안검토 44 M-4).
2. github.com(실패 시 Pages)의 HTTPS `Date` 헤더와 PC 시계를 비교해 **5분 넘게 어긋나면 중단**(M-3).
3. 후보를 앱과 같은 strict 스키마로 검증하고, rc 버전·floor 역전을 거부한다.
4. 후보의 산출물마다 `--artifacts-dir`의 실제 파일로 **크기와 sha256을 다시 계산해 대조**한다(§14.7-4-2). 최종본으로도 한 번 더 대조(L-3).
5. **후보 버전 = 후보 URL 속 태그(`release_page`, 모든 자산 URL의 `v<버전>`) = `--expect-version`** 세 값이 모두 같아야 한다(H-2).
6. **후보 매니페스트와 모든 산출물에 `gh attestation verify`**(repo `yuseungil-a11y/emailtomcp`, signer workflow `release.yml`,
   source ref `refs/tags/v<버전>`, self-hosted runner 거부)를 실행한다. 하나라도 실패하면 중단(H-2) — 같은 draft에서 받은
   후보·산출물이 "서로만 맞는 악성 쌍"이어도 여기서 걸린다.
7. 현재 Pages에 게시된 `stable.json`을 받아 서명이 검증되면 **지금 시각 > 게시본 issued_at**, **새 버전 ≥ 게시본 버전**을 강제한다(M-3).
   게시본이 없으면(최초 릴리스) 건너뛰고, 게시본 서명이 검증되지 않으면 경고만 하고 비교 기준으로 쓰지 않는다.
8. `issued_at`=지금(UTC), `expires`=지금+30일로 채운 `stable.json`을 만든다(키 정렬, LF, UTF-8, 64KB 이하).
9. 서명할 내용(버전, **released_at**, floor, security, release_page, **notes_summary 전문**, 산출물별 slot·이름·크기·sha256·**URL**,
   trusted comment)을 보여주고 `y` 확인을 받는다(`--yes`로 생략 가능 — 첫 stable 릴리스에서는 쓰지 말 것).
10. `minisign -S`를 실행한다. **비밀키 암호는 minisign이 직접 묻는다**(스크립트는 암호를 받거나 저장하지 않으며, 비밀키 경로도 출력하지 않는다).
    trusted comment: `app=EmailToMCP channel=stable version=1.0.1 issued=2026-11-01T00:00:00Z`
11. 결과를 **앱에 내장된 공개키(K1)와 앱의 검증기(`verify_manifest`)로 다시 검증**한다. 다른 키로 서명했으면 여기서 실패한다.
12. 실행마다 새로 만드는 폴더 `release-out/<버전>-<UTC시각>/`(예: `release-out/1.0.1-20261101T000000Z/`)에
    `stable.json`, `stable.json.minisig`를 남긴다(L-1: 예전 실행 결과와 섞인 쌍이 게시되는 것 방지).

## 6. draft → release publish

자산 URL이 살아 있어야 클라이언트가 링크를 열 수 있으므로 **Pages 반영 전에 publish**한다(§14.7-4 순서).
```
gh release edit v1.0.1 --repo yuseungil-a11y/emailtomcp --draft=false
```
(웹: draft 릴리스 편집 → Publish release. Environments 승인 게이트를 쓰는 경우 승인 후.)
`manifest-candidate.json`은 릴리스 자산에 남아 있어도 된다(서명되지 않은 참고 파일이며 클라이언트는 쓰지 않는다).

## 7. Pages에 올릴 파일 → Newton에게 커밋/push 요청

- 올릴 파일: 5단계가 출력한 폴더 `release-out/<버전>-<UTC시각>/`의 `stable.json`, `stable.json.minisig` (2개, **같은 폴더에서 항상 한 쌍으로**)
- 위치: Pages 소스의 `manifest/` 폴더(게시 후 주소 `https://yuseungil-a11y.github.io/emailtomcp/manifest/stable.json`)
- `release-out/`은 `.gitignore` 대상이다. 위 두 파일을 Pages 소스 `manifest/`로 복사한 뒤
  **Newton(vcs-manager)에게 커밋/push를 요청**한다(커밋 type/이슈개요는 사용자 확인).

## 8. 게시 확인

```
curl -fsSLO https://yuseungil-a11y.github.io/emailtomcp/manifest/stable.json
curl -fsSLO https://yuseungil-a11y.github.io/emailtomcp/manifest/stable.json.minisig
minisign -V -P RWRsNFPfSdZbIOJCGCtFny5oT+uyBqtw+zTK/B8Ufs/GFX/Fi2M2K5nC -m stable.json
```
`Signature and comment signature verified`와 trusted comment가 보이면 된다. 이어서 N-1 버전 앱에서
"업데이트 확인"으로 알림이 뜨는지 본다(§15.6 C).

---

## 재서명 (§14.7-5, 30일마다)

`expires`는 30일이다. **만료 7일 전**에 내용은 그대로 두고 `issued_at`/`expires`만 갱신해 다시 서명한다
(만료되면 모든 클라이언트가 "확인 불가"가 된다).

1. 현재 게시본 2개를 받는다(Pages 소스 체크아웃의 `manifest/`를 써도 된다).
2. 실행:
   ```
   python packaging/release/sign_release.py --resign <폴더>\stable.json --expect-version 1.0.1 --key E:\minisign\emailtomcp-K1.key --out-dir release-out
   ```
   - 같은 폴더의 `stable.json.minisig`로 **기존 파일이 K1로 서명된 정상 파일인지 먼저 검증**한다.
     검증이 안 되면 재서명하지 않는다(Pages 소스에 끼어든 변조본을 재서명하는 사고 방지). 서명 파일이 다른 곳에 있으면 `--resign-signature`.
   - **기존 매니페스트 버전 = `--expect-version` = `gh release list`에서 가장 최근 publish된 정식(비-rc) 릴리스 태그**여야 한다.
     예전에 정상 서명됐던 구버전 `stable.json`을 Pages에 되돌려 놓고 재서명을 유도하는 replay를 막는다(M-2).
   - 기존 `expires`가 **이미 지났으면 경고 후 중단**한다(M-2). 만료됐다면 최신 릴리스의 `manifest-candidate.json`과 자산을
     내려받아 5단계 신규 서명 절차로 다시 발행한다.
   - 현재 시각이 기존 `issued_at`보다 늦지 않거나, 서버 시각과 5분 넘게 어긋나면(PC 시계 오류) 중단한다 — 클라이언트가 롤백으로 거부하기 때문이다.
   - 결과는 신규 서명과 같이 `release-out/<버전>-<UTC시각>/`에 생긴다.
3. 7단계처럼 Newton에게 Pages 반영을 요청한다.

## rc 태그

`vX.Y.ZrcN` 태그는 빌드와 draft(prerelease) 릴리스까지만 만들고 **후보 매니페스트는 만들지 않는다**.
stable 채널 매니페스트에는 rc를 넣을 수 없고(클라이언트가 거부, §14.6 #6), rc 채널 배포는 U2/P5 범위다.
Velopack도 rc는 `rc` 채널로 패키징해 stable 피드와 섞이지 않게 한다.

## 현재 한계 (2026-10-05)

- **저장소 설정 3가지(Environment `release` 승인자, `v*` 태그 ruleset, main·Pages 브랜치 보호)는 아직 적용되지 않았다.**
  작성 PC의 `gh`가 로그인돼 있지 않아 `gh api`로 ruleset을 만들지 못했다 — 0단계 "저장소 설정"대로 관리자가 직접 적용해야 한다.
  특히 `v*` 태그 ruleset은 rc 시험 태그를 push하기 **전에** 적용한다(보안검토 44 조건부 Go 조건).

- **`vpk pack`(Velopack) 로컬 빌드 검증 완료(2026-10-05).** 로컬 PC에 `vpk`를 설치해 `build_velopack.py`로
  실제 `Setup.exe`를 생성하고, 설치·기동·제거까지 확인했다(약 31초 소요). 이 과정에서 버그를 발견해 수정했다 —
  `vpk pack`에 `--runtime`을 명시하지 않으면 vpk가 architecture를 x86으로 기본설정해 preprocess 스테이징 폴더
  복사 단계에서 `System.UnauthorizedAccessException`이 발생했다(로컬 5회 연속 재현). GitHub Actions의
  `release.yml`은 rc1 테스트에서 이미 성공했으나(클라우드 환경 차이로 우연히 문제가 드러나지 않은 것으로 보임)
  환경에 의존하지 않도록 `build_vpk_command()`가 항상 `--runtime win-x64`를 고정으로 넘기게 고쳤다(이 프로젝트는
  현재 win-x64만 타겟, `release.yml` matrix `arch: x64`). minisign 서명·전체 release.yml 워크플로는 아직
  실제로 돌려 보지 못했다 — 첫 릴리스(또는 `v1.0.0rc1` 같은 시험 태그) 때 각 단계 출력 파일명을 확인하고 이 문서를
  보정한다. 특히 Velopack 출력 파일명(`EmailToMCP-stable-Setup.exe`, `EmailToMCP-<semver>-stable-full.nupkg` 등)은
  `release_common.classify_file`의 접미사 규칙(`-Setup.exe`, `-full.nupkg`, `-delta.nupkg`, `releases.<채널>.json`)으로만 판단한다.
- 델타 패키지는 아직 만들지 않는다(이전 릴리스 full nupkg를 `vpk download`로 받아 둬야 생성됨). 스키마·스크립트는 `velopack_delta`를 지원한다.
- Windows 창 모드(`console=False`) exe는 `--version` 출력이 비어 있을 수 있어, 워크플로 스모크는 이 경우 종료 코드만 확인하고 경고를 남긴다.
- SBOM은 `pip list --format=json` 수준이다(CycloneDX는 후속).
- 코드서명(Authenticode/notarization)은 하지 않는다(K4, §14.8).
