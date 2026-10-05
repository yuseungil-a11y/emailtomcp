"""출력 가드 (DESIGN.md §7.5 H7) — P3 Phase B.

`check(...) -> GuardResult`(frozen). **GuardResult는 이 모듈만 생성한다**(아키텍처 테스트).

Phase B의 결과물은 모두 사람이 검토하는 초안이라 §7.5에 따라 출력 가드로 강등하지 않는다 —
대신 결과(findings)를 auto_reply_log와 작성 창 경고 근거로 남긴다. 해시(`body_sha256`)는 G7 ⑦
본문 동일성 대조에 쓰인다. auto_send가 열리는 Phase C에서는 `ok=False`면 무조건 강등한다.

탐지(정규화: NFKC, 제로폭·bidi 제거, 연속 공백 정리 후)
- url: 스킴, `www.`, 도메인 형태, 난독화(`hxxp`, `[.]`, `(dot)`, 전각 점은 NFKC로 흡수), punycode
- email: `@`, `[at]`/`(at)`/` at ` 난독화
- phone: 국내 `0\\d{1,2}-\\d{3,4}-\\d{4}`, `+82`, 국제 형식
- account_number: 숫자 10자리 이상(하이픈·공백 포함), 은행명 + 숫자열
- money: 통화기호·단위 + 숫자
- promise: 확정·약속 문구(상수 목록, 부분일치)
- quote_line: `>`로 시작하는 줄 / original_copy: 원문과 공통 연속 부분문자열 40자 이상
- empty_body / too_long(max_chars 초과) / banned_word(사용자 금칙어)
- subject_url: 원문 제목에 URL·이메일
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable

from emailtomcp.core.autoreply_types import GuardResult

_INVISIBLE_RE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_SPACE_RE = re.compile(r"[ \t ]+")

_TLDS = (
    "com|net|org|kr|co|io|me|info|biz|xyz|app|dev|ai|jp|cn|us|uk|de|fr|ru|edu|gov|mil|int|"
    "site|online|shop|store|link|top|club|tv|ly|gl|gg|to|cc|ws|in|eu|asia|tech|page"
)
_URL_RES = (
    re.compile(r"[a-z][a-z0-9+.\-]{1,20}://", re.IGNORECASE),
    re.compile(r"\bwww\s*\.", re.IGNORECASE),
    re.compile(r"\bhxxps?\b", re.IGNORECASE),
    re.compile(r"\[\s*\.\s*\]|\(\s*\.\s*\)|\(\s*dot\s*\)|\[\s*dot\s*\]|\s+dot\s+", re.IGNORECASE),
    re.compile(r"\bxn--[a-z0-9\-]+", re.IGNORECASE),
    re.compile(rf"\b[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*\.(?:{_TLDS})\b", re.IGNORECASE),
)
_EMAIL_RES = (
    re.compile(r"[^\s@]+@[^\s@]+"),
    re.compile(r"\S+\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\s+at\s+)\s*\S+\.\S+", re.IGNORECASE),
)
_PHONE_RES = (
    re.compile(r"(?<!\d)0\d{1,2}[- .]?\d{3,4}[- .]?\d{4}(?!\d)"),
    re.compile(r"\+\s?82"),
    re.compile(r"\+\s?\d{1,3}[- .]?\(?\d{1,4}\)?[- .]?\d{3,4}[- .]?\d{3,4}"),
)
_ACCOUNT_RES = (
    re.compile(r"(?:\d[- ]?){10,}"),
    re.compile(r"(?:은행|뱅크|bank)\D{0,10}\d{3,}", re.IGNORECASE),
)
# 금액 탐지(데카르트 33번 L-8 / Spinoza 34번 N-4 보강).
# - 숫자는 숫자로 끝나야 한다(`.`·공백을 함부로 삼키지 않게 — "1. 원인 분석" 오탐 방지). 숫자
#   연속열의 중간에서 시작하지 않도록 앞을 막는다(짧은 숫자 규칙이 긴 숫자 꼬리에 걸리지 않게,
#   또 시작 위치를 숫자열 머리로 한정해 백트래킹을 선형으로 묶는다).
# - 한글 단위 뒤에는 조사가 바로 붙는다("1,500,000원을", "100만원입니다") — `\b`는 한글끼리
#   경계가 없어 쓰지 않는다. 영문 단위만 뒤에 영문자가 이어지지 않는지 본다.
# - 탐지 누락은 Phase C에서 자동발송 우회 경로가 되므로(ok=False 강등 근거) 넓게 잡고, 오탐은
#   "숫자+원으로 시작하는 낱말"처럼 뚜렷한 경우만 제외한다.
_NUM = r"(?<![\d,.])\d(?:[\d,]*\d)?(?:\.\d+)?"
# 1~2자리 숫자 바로 뒤 '원'이 낱말의 시작인 경우(제3원칙, 4원소, 2원화, 회의실 3 원격, 2.0원문).
# 3자리 이상·쉼표 있는 숫자 뒤의 '원'은 이 목록과 무관하게 금액으로 본다("1500원인데").
_WON_WORD_AFTER = (
    "인|본|문|활|칙|소|격|하|화|래|리|형|자|료|조|작|"
    "고|서|시|주|산|수|론|색|탁|숭|생|성|예|경|근|동|반|년"
)
# 단독 만/억 뒤에 오면 금액이 아닌 말(2억년, 3억제, 100만 명, 1만일 ...).
_MAN_EOK_NOT_MONEY = (
    "년|명|개|건|회|번|배|제|측|지|압|울|약|일|큼|족|"
    "세|점|분|곳|톤|평|권|표|호|층|살|대|마리|가구|[a-zA-Z]"
)
_KNUM_CHARS = "일이삼사오육칠팔구십백천만억조"
_MONEY_RES = (
    re.compile(r"[₩$€£¥]\s?\d"),
    # 단위가 붙은 금액: 5천원, 100만원, 100만 원, 1백만원, 2억 원, 3천만 달러
    re.compile(rf"{_NUM}\s?(?:[십백천]?[만억조]|[십백천])\s?(?:원|달러)"),
    # 단독 만/억(원 생략): 100만, 2억 (뒤에 년·명 등 다른 단위가 오면 제외)
    re.compile(rf"{_NUM}\s?[십백천]?[만억조](?!\s?(?:{_MAN_EOK_NOT_MONEY}))"),
    # 3자리 이상 또는 쉼표가 있는 숫자 + 원: 1,500,000원을, 1500원
    re.compile(r"(?<![\d,.])(?:\d{3}|\d{1,2},)[\d,]*(?:\.\d+)?\s?원"),
    # 1~2자리 숫자 + 원: '원'으로 시작하는 낱말이면 제외
    re.compile(rf"(?<![\d,.])\d{{1,2}}(?:\.\d+)?\s?원(?!{_WON_WORD_AFTER})"),
    re.compile(rf"{_NUM}\s?달러"),
    # 한글 수사 + 원/달러: 일백만원, 오십만 원, 천원, 이십오원 — 십·백·천·만·억 중 하나는 있어야
    # 한다(사원·구원·일원 같은 낱말 제외). 낱말 중간("불만 원인")에서 시작하지 않되, 앞이 조사나
    # "금"(금 일백만원정)이면 허용한다. 길이를 묶어 백트래킹을 제한한다.
    re.compile(
        rf"(?:(?<![가-힣])|(?<=[이가은는을를에의로도금]))"
        rf"(?=[{_KNUM_CHARS}]{{0,29}}[십백천만억])[{_KNUM_CHARS}]{{1,30}}\s?(?:원|달러)"
    ),
    re.compile(rf"{_NUM}\s?(?:usd|krw|eur|jpy)(?![a-z])", re.IGNORECASE),
    re.compile(r"\b(?:usd|krw|eur|jpy)\s?\d", re.IGNORECASE),
)
PROMISE_PHRASES: tuple[str, ...] = (
    "확정",
    "승인",
    "계약",
    "입금",
    "송금",
    "이체",
    "결제",
    "계좌 변경",
    "계좌변경",
    "보장",
    "약속드립니다",
    "납품 확정",
    "견적 승인",
    "환불",
)
QUOTE_MIN_CHARS = 40


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE_RE.sub("", text)
    return _SPACE_RE.sub(" ", text)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _any(patterns: Iterable[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def _shares_long_substring(reply: str, original: str, size: int = QUOTE_MIN_CHARS) -> bool:
    """reply와 original이 `size`자 이상 연속으로 겹치는지(공백 정리 후, 대소문자 무시)."""
    a = " ".join(reply.split()).casefold()
    b = " ".join(original.split()).casefold()
    if len(a) < size or len(b) < size:
        return False
    windows = {b[i : i + size] for i in range(len(b) - size + 1)}
    return any(a[i : i + size] in windows for i in range(len(a) - size + 1))


def check(
    *,
    body: str,
    subject: str,
    original_body: str | None,
    original_subject: str | None,
    max_chars: int,
    banned_words: Iterable[str] = (),
) -> GuardResult:
    """제출 본문(과 정화한 제목)을 검사한다. 해시는 정규화 전 **원래 본문** 기준이다(G7 ⑦)."""
    text = _normalize(body)
    findings: list[str] = []
    if not text.strip():
        findings.append("empty_body")
    if len(body) > max_chars:
        findings.append("too_long")
    if _any(_URL_RES, text):
        findings.append("url")
    if _any(_EMAIL_RES, text):
        findings.append("email")
    if _any(_PHONE_RES, text):
        findings.append("phone")
    if _any(_ACCOUNT_RES, text):
        findings.append("account_number")
    if _any(_MONEY_RES, text):
        findings.append("money")
    folded = text.casefold()
    if any(phrase in folded for phrase in PROMISE_PHRASES):
        findings.append("promise")
    if any(line.lstrip().startswith(">") for line in body.splitlines()):
        findings.append("quote_line")
    if original_body and _shares_long_substring(text, _normalize(original_body[:10_000])):
        findings.append("original_copy")
    words = [w.strip().casefold() for w in banned_words if w and w.strip()]
    if any(w in folded for w in words):
        findings.append("banned_word")
    if original_subject:
        subj = _normalize(original_subject)
        if _any(_URL_RES, subj) or _any(_EMAIL_RES, subj):
            findings.append("subject_url")
    return GuardResult(
        ok=not findings,
        findings=tuple(findings),
        body_sha256=_sha(body),
        subject_sha256=_sha(subject),
    )
