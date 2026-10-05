# 릴리스 절차 (빌드 → 오프라인 서명 → Pages 반영)

근거: `docs/DESIGN.md` §14.3(Velopack), §14.4(매니페스트 스펙), §14.7(서명키 운영), §15.4(release.yml), §15.6(릴리스 체크리스트).

## 역할 분리 (반드시 지킬 것)

| 단계 | 어디서 | 누가 | 비밀키 |
|---|---|---|---|
| 빌드, SHA256, SBOM, provenance, **서명 전 후보 매니페스트**, draft 릴리스 업로드 | GitHub Actions(`.github/workflows/release.yml`) | 자동 | **없음** |
| 산출물 sha256 재대조 + `stable.json` 서명 | 메인테이너 PC(`sign_release.py`) | 메인테이너 | 오프라인 USB |
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
| `sign_release.py` | **메인테이너 PC만** | 산출물 재대조 → `stable.json` 생성 → `minisign` 서명 → 앱 검증기로 자체검증. `--resign`으로 재서명 |

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
   (`sign_release.py`는 비밀키가 저장소 작업트리 안에 있으면 실행을 거부한다).
4. (선택) GitHub CLI `gh` — draft 자산 다운로드와 publish에 쓴다(`gh auth login`).

### 저장소 설정 (1회, 관리자)
- GitHub Pages 활성화. 매니페스트 게시 주소는 `https://yuseungil-a11y.github.io/emailtomcp/manifest/`
  (`update/release_client.py`의 `DEFAULT_BASE_URL`). Pages 소스(예: `gh-pages` 브랜치 루트, 또는 main의 `/docs`)
  아래 `manifest/` 폴더가 이 주소에 대응한다 — 어느 쪽인지 확정되면 이 문서에 적어 둔다.
- `v*` 태그 보호 ruleset, Actions 권한(워크플로가 `contents: write`로 draft 릴리스를 만든다), §15.5 참고.

### Velopack CLI (CI가 자동 설치, 로컬 빌드 시에만 필요)
```
dotnet tool install -g vpk --version 1.2.161
```
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

저장소 → Actions → **Release** 워크플로에서 3개 job을 확인한다.

| job | 하는 일 | 실패하면 |
|---|---|---|
| `validate` | 태그 정규식, 작업트리 clean, `pip install -e .`로 생성된 `_version.py` == 태그(`+`/`.dev` 없음) | 태그가 잘못됐거나 태그 커밋이 아님 → 새 버전 번호로 다시(태그 이동 금지) |
| `build` (windows-latest, macos-latest) | PyInstaller → `--version` 스모크 → (Windows) `vpk pack` / (macOS) onedir zip → SBOM(`pip list` JSON) | 로그 확인 후 수정, 새 태그로 다시 |
| `draft-release` | `manifest-candidate.json` 생성(rc 태그는 생략), 자산 평탄화 + `SHA256SUMS.txt`, provenance, **draft 릴리스** 생성/업로드 | 같은 태그로 재실행하면 draft에 덮어쓴다(publish된 릴리스는 거부) |

워크플로는 **서명도 publish도 하지 않는다.** 끝나면 Releases에 draft가 하나 생긴다.

## 4. draft 자산 내려받기

```
gh release download v1.0.1 --repo yuseungil-a11y/emailtomcp --dir D:\release\v1.0.1
```
(웹에서 draft를 열어 자산을 모두 받아 한 폴더에 두어도 된다.) 폴더에 `manifest-candidate.json`,
`EmailToMCP-...-Setup.exe`, `...-full.nupkg`, `releases.stable.json`, `EmailToMCP-1.0.1-macos-arm64.zip`,
`SHA256SUMS.txt`, `sbom-pip-*.json` 등이 있어야 한다.

(선택) 빌드 증명 확인: `gh attestation verify D:\release\v1.0.1\EmailToMCP-stable-Setup.exe --repo yuseungil-a11y/emailtomcp`

## 5. 오프라인 서명 (메인테이너 PC, 비밀키 USB 연결)

레포 루트에서:
```
python packaging/release/sign_release.py ^
    --candidate D:\release\v1.0.1\manifest-candidate.json ^
    --artifacts-dir D:\release\v1.0.1 ^
    --key E:\minisign\emailtomcp-K1.key ^
    --out-dir release-out
```
(macOS/리눅스 셸은 줄 끝 `^` 대신 `\`.)

스크립트가 하는 일(하나라도 실패하면 중단하고 결과 파일을 남기지 않는다):
1. 후보를 앱과 같은 strict 스키마로 검증하고, rc 버전·floor 역전을 거부한다.
2. 후보의 산출물마다 `--artifacts-dir`의 실제 파일로 **크기와 sha256을 다시 계산해 대조**한다(§14.7-4-2).
3. `issued_at`=지금(UTC), `expires`=지금+30일로 채운 `stable.json`을 만든다(키 정렬, LF, UTF-8, 64KB 이하).
4. 서명할 내용(버전, floor, 산출물 해시, trusted comment)을 보여주고 `y` 확인을 받는다(`--yes`로 생략 가능).
5. `minisign -S`를 실행한다. **비밀키 암호는 minisign이 직접 묻는다**(스크립트는 암호를 받거나 저장하지 않으며, 비밀키 경로도 출력하지 않는다).
   trusted comment: `app=EmailToMCP channel=stable version=1.0.1 issued=2026-11-01T00:00:00Z`
6. 결과를 **앱에 내장된 공개키(K1)와 앱의 검증기(`verify_manifest`)로 다시 검증**한다. 다른 키로 서명했으면 여기서 실패한다.
7. `release-out/stable.json`, `release-out/stable.json.minisig`를 남긴다.

## 6. draft → release publish

자산 URL이 살아 있어야 클라이언트가 링크를 열 수 있으므로 **Pages 반영 전에 publish**한다(§14.7-4 순서).
```
gh release edit v1.0.1 --repo yuseungil-a11y/emailtomcp --draft=false
```
(웹: draft 릴리스 편집 → Publish release. Environments 승인 게이트를 쓰는 경우 승인 후.)
`manifest-candidate.json`은 릴리스 자산에 남아 있어도 된다(서명되지 않은 참고 파일이며 클라이언트는 쓰지 않는다).

## 7. Pages에 올릴 파일 → Newton에게 커밋/push 요청

- 올릴 파일: `release-out/stable.json`, `release-out/stable.json.minisig` (2개, 항상 한 쌍으로)
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
   python packaging/release/sign_release.py --resign <폴더>\stable.json --key E:\minisign\emailtomcp-K1.key --out-dir release-out
   ```
   - 같은 폴더의 `stable.json.minisig`로 **기존 파일이 K1로 서명된 정상 파일인지 먼저 검증**한다.
     검증이 안 되면 재서명하지 않는다(Pages 소스에 끼어든 변조본을 재서명하는 사고 방지). 서명 파일이 다른 곳에 있으면 `--resign-signature`.
   - 현재 시각이 기존 `issued_at`보다 늦지 않으면(PC 시계 오류) 중단한다 — 클라이언트가 롤백으로 거부하기 때문이다.
3. 7단계처럼 Newton에게 Pages 반영을 요청한다.

## rc 태그

`vX.Y.ZrcN` 태그는 빌드와 draft(prerelease) 릴리스까지만 만들고 **후보 매니페스트는 만들지 않는다**.
stable 채널 매니페스트에는 rc를 넣을 수 없고(클라이언트가 거부, §14.6 #6), rc 채널 배포는 U2/P5 범위다.
Velopack도 rc는 `rc` 채널로 패키징해 stable 피드와 섞이지 않게 한다.

## 현재 한계 (2026-10-05)

- **실제 빌드로 검증하지 않았다.** 작성 환경에 `vpk`와 `minisign`이 없어 `vpk pack`·minisign 서명·워크플로를
  실제로 돌려 보지 못했다. 첫 릴리스(또는 `v1.0.0rc1` 같은 시험 태그) 때 각 단계 출력 파일명을 확인하고 이 문서를 보정한다.
  특히 Velopack 출력 파일명(`EmailToMCP-stable-Setup.exe`, `EmailToMCP-<semver>-stable-full.nupkg` 등)은
  `release_common.classify_file`의 접미사 규칙(`-Setup.exe`, `-full.nupkg`, `-delta.nupkg`, `releases.<채널>.json`)으로만 판단한다.
- 델타 패키지는 아직 만들지 않는다(이전 릴리스 full nupkg를 `vpk download`로 받아 둬야 생성됨). 스키마·스크립트는 `velopack_delta`를 지원한다.
- Windows 창 모드(`console=False`) exe는 `--version` 출력이 비어 있을 수 있어, 워크플로 스모크는 이 경우 종료 코드만 확인하고 경고를 남긴다.
- SBOM은 `pip list --format=json` 수준이다(CycloneDX는 후속).
- 코드서명(Authenticode/notarization)은 하지 않는다(K4, §14.8).
