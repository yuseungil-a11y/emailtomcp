# build_velopack.py --runtime 명시 보강 — Edison

## 배경
로컬 설치패키지 생성테스트중 발견: `vpk pack`명령에 `--runtime`인자없으면 vpk가architecture를x86으로기본설정하고 preprocess스테이징폴더복사단계서`System.UnauthorizedAccessException`발생(로컬PC5회연속재현). `--runtime win-x64`명시하면완전해결(31초만에정상완료). release.yml도같은스크립트쓰지만 rc1테스트에선이미성공(클라우드환경차이로문제안생긴것으로보임) — 환경의존없이항상안전하게동작하도록명시적수정.

## 수정내용
1. `build_velopack.py`: `RUNTIME="win-x64"`상수추가(주석에버그원인·재현조건기록)+`build_vpk_command()`가구성하는vpk pack명령에`--runtime win-x64`고정인자추가. 이스크립트자체엔Windows/비Windows분기없고"Windows에서만호출"하는분기는release.yml의matrix.platform=='windows'조건에있음 — build_vpk_command()는애초Windows전용명령만들어 플랫폼분기없이바로고정값추가. ARM64검토: release.yml matrix는os:windows-latest/platform:windows/arch:x64 한종류뿐, README도win-x64만언급 — 현재범위는x64단일타겟확인, 호스트아키텍처감지없이win-x64하드코딩.
2. `README.md`(277행근처"현재한계"): "실제빌드로검증안했다"문구를 이번로컬검증결과(vpk pack로컬빌드완료-Setup.exe생성·설치·기동·제거확인,약31초, --runtime누락시UnauthorizedAccessException재현·수정경위)로갱신. minisign서명·전체release.yml워크플로는아직미검증이라는점은유지.
3. `test_release_scripts.py`: `test_vpk_command_includes_explicit_runtime`회귀테스트추가(build_vpk_command()반환값에--runtime win-x64포함확인, 실제vpk실행없음).

## 테스트결과
`test_release_scripts.py` **68 passed**(기존67+신규1).

## 실제재검증
로컬설치된vpk(Velopack CLI1.2.161)로 기존dist/emailtomcp/재사용해 `build_velopack.py --pack-dir dist/emailtomcp --output-dir vpk-out-verify --version 1.0.0` 실행 — UnauthorizedAccessException없이32초(`Finished in 00:00:32.18`)에정상완료, `--runtime win-x64`가실제명령줄에포함됨을로그확인(`vpk.EXE pack --packId EmailToMCP ... --mainExe emailtomcp.exe --runtime win-x64 --packTitle ...`). Setup.exe/full nupkg/Portable.zip등산출물정상생성확인후검증용폴더는삭제(dist/emailtomcp/는보존).

## 변경 파일
`packaging/release/build_velopack.py`, `packaging/release/README.md`, `tests/unit/test_release_scripts.py`

커밋 안함.
