"""한글 인코딩 `.eml` 테스트 fixture 생성 스크립트 (갈릴레오 §5 한글 fixture).

QA BUG-① 재현용 "RFC 2047 미인코딩 8비트 헤더" fixture 2종(From/Subject)과,
데카르트 재검증 D-1/D-2 재현용 "8비트 첨부 파트 헤더" fixture 2종
(Content-Disposition·Content-ID / Content-Type name=)도 포함한다.
이 디렉터리의 `*.eml` 파일들은 사람이 직접 바이트를 손으로 쓰기 어려운
cp949/euc-kr 인코딩을 포함하므로, 코드로 생성해 커밋해 둔다. 스키마나 인코딩
케이스를 추가/수정할 때는 이 스크립트를 고쳐서 다시 실행한다:

    python tests/fixtures/eml/generate_fixtures.py
"""

from __future__ import annotations

import base64
import quopri
from pathlib import Path

HERE = Path(__file__).parent


def _write(name: str, raw: bytes) -> None:
    (HERE / name).write_bytes(raw)


def _b_encoded_word(text: str, charset: str) -> str:
    encoded = base64.b64encode(text.encode(charset)).decode("ascii")
    return f"=?{charset}?B?{encoded}?="


def _q_encoded_word(text: str, charset: str) -> str:
    encoded = quopri.encodestring(text.encode(charset), header=True).decode("ascii")
    return f"=?{charset}?Q?{encoded}?="


def gen_utf8_plain() -> None:
    """UTF-8 본문 + ASCII 제목(기준 케이스)."""
    body = "안녕하세요. UTF-8 본문 테스트입니다.\r\n한글이 깨지지 않아야 합니다.\r\n"
    raw = (
        b"From: sender@example.com\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: UTF-8 plain test\r\n"
        b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        b"Message-ID: <utf8-plain-001@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + body.encode("utf-8")
    )
    _write("utf8_plain.eml", raw)


def gen_euckr_plain() -> None:
    """EUC-KR 본문(charset=euc-kr)."""
    body = "안녕하세요. EUC-KR 본문 테스트입니다.\r\n"
    raw = (
        b"From: sender@example.com\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: EUC-KR plain test\r\n"
        b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        b"Message-ID: <euckr-plain-002@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=euc-kr\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + body.encode("euc_kr")
    )
    _write("euckr_plain.eml", raw)


def gen_cp949_plain() -> None:
    """CP949 본문(charset=cp949, 확장 완성형 포함)."""
    body = "안녕하세요. CP949 확장 완성형 테스트: 뀨뀰 쀍 테스트입니다.\r\n"
    raw = (
        b"From: sender@example.com\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: CP949 plain test\r\n"
        b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        b"Message-ID: <cp949-plain-003@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=cp949\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + body.encode("cp949")
    )
    _write("cp949_plain.eml", raw)


def gen_ks_c_5601_plain() -> None:
    """비표준 charset 라벨 `ks_c_5601-1987`(실제 바이트는 cp949로 인코딩, §9.4 별칭 매핑)."""
    body = "안녕하세요. ks_c_5601-1987 라벨 별칭 매핑 테스트입니다.\r\n"
    raw = (
        b"From: sender@example.com\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: ks_c_5601-1987 alias test\r\n"
        b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        b"Message-ID: <ksc5601-plain-004@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=ks_c_5601-1987\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + body.encode("cp949")
    )
    _write("ks_c_5601_plain.eml", raw)


def gen_rfc2231_attachment() -> None:
    """RFC 2231 인코딩 첨부파일명(`filename*=UTF-8''...`, percent-encoding)."""
    # "한글파일.txt"를 UTF-8 percent-encoding으로 표현한다.
    encoded_name = "".join(f"%{b:02X}" for b in "한글파일.txt".encode())
    body_text = b"Hello, this message has an RFC2231-encoded attachment filename.\r\n"
    attachment_payload = base64.b64encode("첨부 내용입니다.\r\n".encode()).decode("ascii")
    raw = (
        b"From: sender@example.com\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: RFC2231 attachment filename test\r\n"
        b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        b"Message-ID: <rfc2231-attach-005@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="BOUNDARY005"\r\n'
        b"\r\n"
        b"--BOUNDARY005\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + body_text + b"\r\n"
        b"--BOUNDARY005\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"Content-Disposition: attachment;\r\n"
        b" filename*=UTF-8''" + encoded_name.encode("ascii") + b"\r\n"
        b"\r\n" + attachment_payload.encode("ascii") + b"\r\n"
        b"--BOUNDARY005--\r\n"
    )
    _write("rfc2231_attachment.eml", raw)


def gen_nonstandard_encoded_word_filename() -> None:
    """비표준: `filename=`에 RFC 2231이 아니라 RFC 2047 encoded-word를 바로 쓴 경우
    (구형 한국 메일 클라이언트에서 흔함).
    """
    encoded_word = _b_encoded_word("견적서.xlsx", "EUC-KR")
    body_text = b"Non-standard encoded-word filename test.\r\n"
    attachment_payload = base64.b64encode("dummy content".encode("ascii")).decode("ascii")
    raw = (
        b"From: sender@example.com\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: Non-standard encoded-word filename test\r\n"
        b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        b"Message-ID: <nonstd-encword-006@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="BOUNDARY006"\r\n'
        b"\r\n"
        b"--BOUNDARY006\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + body_text + b"\r\n"
        b"--BOUNDARY006\r\n"
        b"Content-Type: application/vnd.ms-excel\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b'Content-Disposition: attachment; filename="' + encoded_word.encode("ascii") + b'"\r\n'
        b"\r\n" + attachment_payload.encode("ascii") + b"\r\n"
        b"--BOUNDARY006--\r\n"
    )
    _write("nonstandard_encoded_word_filename.eml", raw)


def gen_folded_subject() -> None:
    """제목이 여러 줄로 폴딩되고, 각 줄이 서로 다른 encoded-word인 경우."""
    part1 = _b_encoded_word("매우 길고 긴 제목의 앞부분입니다 ", "UTF-8")
    part2 = _b_encoded_word("그리고 이어지는 뒷부분입니다", "UTF-8")
    body = "제목 폴딩(folding) 테스트 본문입니다.\r\n"
    raw = (
        b"From: sender@example.com\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: " + part1.encode("ascii") + b"\r\n"
        b" " + part2.encode("ascii") + b"\r\n"
        b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        b"Message-ID: <folded-subject-007@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + body.encode("utf-8")
    )
    _write("folded_subject.eml", raw)


def gen_content_mismatch() -> None:
    """plain과 html 본문이 크게 다른 경우(§8.4 content_mismatch=1 판정 대상)."""
    plain_body = "이것은 평문 버전입니다. 안내: 송금하지 마세요. 공식 안내문입니다.\r\n"
    html_body = (
        "<html><body><p>완전히 다른 내용의 HTML 버전입니다. "
        "숨겨진 광고 문구와 전혀 무관한 텍스트가 들어갑니다. 구매 링크 안내.</p></body></html>\r\n"
    )
    raw = (
        b"From: sender@example.com\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: Content mismatch test\r\n"
        b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        b"Message-ID: <content-mismatch-008@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="BOUNDARY008"\r\n'
        b"\r\n"
        b"--BOUNDARY008\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + plain_body.encode("utf-8") + b"\r\n"
        b"--BOUNDARY008\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + html_body.encode("utf-8") + b"\r\n"
        b"--BOUNDARY008--\r\n"
    )
    _write("content_mismatch.eml", raw)


def gen_raw8bit_utf8_header() -> None:
    """From/Subject가 RFC 2047 encoded-word 없이 UTF-8 바이트 그대로 들어있는 경우
    (RFC 6532 SMTPUTF8, 일부 국내 레거시 발송 시스템에서 실제 발생 — QA BUG-① 재현).

    `email.message_from_bytes`는 이런 헤더를 `str`이 아니라 `email.header.Header`
    객체로 돌려주므로, `_unfold`가 이를 안전하게 처리하는지 확인하는 fixture다.
    """
    from_name = "미안".encode()
    subject = "한글 제목 테스트 메일입니다".encode()
    body = "안녕하세요 본문입니다.\r\n"
    raw = (
        b"From: " + from_name + b" <sender@example.com>\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: " + subject + b"\r\n"
        b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        b"Message-ID: <raw8bit-utf8-009@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + body.encode("utf-8")
    )
    _write("raw8bit_utf8_header.eml", raw)


def gen_raw8bit_euckr_header() -> None:
    """From/Subject가 RFC 2047 encoded-word 없이 EUC-KR 바이트 그대로 들어있는 경우
    (QA BUG-① 권고 2종 중 EUC-KR 버전). charset_normalizer의 짧은 바이트열 추정
    오류를 피하기 위해 From/Subject 모두 충분히 긴 한글 문구를 사용한다.
    """
    from_name = "한국도로공사 담당자".encode("euc-kr")
    subject = "한국도로공사 담당자 앞 테스트 메일 제목".encode("euc-kr")
    body = "본문 내용입니다.\r\n".encode("euc-kr")
    raw = (
        b"From: " + from_name + b" <sender2@example.com>\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: " + subject + b"\r\n"
        b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        b"Message-ID: <raw8bit-euckr-010@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=euc-kr\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + body
    )
    _write("raw8bit_euckr_header.eml", raw)


def gen_raw8bit_attachment_filename() -> None:
    """첨부 파트의 Content-Disposition/Content-ID에 RFC 2047 인코딩 없이 UTF-8
    바이트가 그대로 들어있는 경우(QA D-1 재현 — 국내 레거시 클라이언트가 실제로
    이렇게 보냄). `parse.py`의 Content-Disposition·Content-ID 처리 두 곳 모두
    `_unfold`로 보호되어 `parse_limited=False`로 정상 파싱되는지 확인한다.
    """
    filename_bytes = "한글문서.pdf".encode()
    content_id_bytes = "첨부식별자".encode()
    body_text = b"Raw 8bit attachment filename/content-id test.\r\n"
    attachment_payload = base64.b64encode(b"dummy pdf content").decode("ascii")
    raw = (
        b"From: sender@example.com\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: Raw 8bit attachment filename test\r\n"
        b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        b"Message-ID: <raw8bit-attach-011@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="BOUNDARY011"\r\n'
        b"\r\n"
        b"--BOUNDARY011\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + body_text + b"\r\n"
        b"--BOUNDARY011\r\n"
        b"Content-Type: application/pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b'Content-Disposition: attachment; filename="' + filename_bytes + b'"\r\n'
        b"Content-ID: <" + content_id_bytes + b">\r\n"
        b"\r\n" + attachment_payload.encode("ascii") + b"\r\n"
        b"--BOUNDARY011--\r\n"
    )
    _write("raw8bit_attachment_filename.eml", raw)


def gen_raw8bit_contenttype_filename() -> None:
    """파일명이 Content-Disposition이 아니라 Content-Type의 `name=` 파라미터에만
    있고, 그 값이 RFC 2047/2231 인코딩 없이 UTF-8 바이트 그대로 들어있는 경우
    (QA D-2 재현). 크래시는 없지만 `_attachment_filename`이 깨진 문자열이 아니라
    올바르게 재디코딩된 파일명을 반환하는지 확인한다.
    """
    filename_bytes = "첨부사진.jpg".encode()
    body_text = b"Raw 8bit content-type filename test.\r\n"
    attachment_payload = base64.b64encode(b"dummy jpg content").decode("ascii")
    raw = (
        b"From: sender@example.com\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: Raw 8bit content-type filename test\r\n"
        b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        b"Message-ID: <raw8bit-ctfilename-012@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="BOUNDARY012"\r\n'
        b"\r\n"
        b"--BOUNDARY012\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + body_text + b"\r\n"
        b"--BOUNDARY012\r\n"
        b'Content-Type: image/jpeg; name="' + filename_bytes + b'"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + attachment_payload.encode("ascii") + b"\r\n"
        b"--BOUNDARY012--\r\n"
    )
    _write("raw8bit_contenttype_filename.eml", raw)


def gen_raw8bit_cd_ascii_ct_8bit_filename_priority() -> None:
    """Content-Disposition filename은 순수 ASCII이고 Content-Type name은 RFC 2047/2231
    인코딩 없이 8비트 바이트 그대로인 경우(QA R-1 재현). `Message.get_filename()`과
    같이 **Content-Disposition을 우선**해야 하는데, 수정 전 코드는 Content-Type의
    8비트 헬퍼가 먼저 값을 돌려줘 Content-Type 쪽(다른이름.pdf)이 선택됐다.
    """
    other_name_bytes = "다른이름.pdf".encode()
    body_text = b"Content-Disposition(ASCII) vs Content-Type(8bit) filename priority test.\r\n"
    attachment_payload = base64.b64encode(b"dummy pdf content").decode("ascii")
    raw = (
        b"From: sender@example.com\r\n"
        b"To: receiver@example.com\r\n"
        b"Subject: CD ascii vs CT 8bit filename priority test\r\n"
        b"Date: Mon, 1 Sep 2025 09:00:00 +0900\r\n"
        b"Message-ID: <raw8bit-cd-ascii-ct-8bit-013@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="BOUNDARY013"\r\n'
        b"\r\n"
        b"--BOUNDARY013\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n"
        b"\r\n" + body_text + b"\r\n"
        b"--BOUNDARY013\r\n"
        b'Content-Type: application/pdf; name="' + other_name_bytes + b'"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b'Content-Disposition: attachment; filename="ascii.pdf"\r\n'
        b"\r\n" + attachment_payload.encode("ascii") + b"\r\n"
        b"--BOUNDARY013--\r\n"
    )
    _write("raw8bit_cd_ascii_ct_8bit_filename_priority.eml", raw)


def main() -> None:
    gen_utf8_plain()
    gen_euckr_plain()
    gen_cp949_plain()
    gen_ks_c_5601_plain()
    gen_rfc2231_attachment()
    gen_nonstandard_encoded_word_filename()
    gen_folded_subject()
    gen_content_mismatch()
    gen_raw8bit_utf8_header()
    gen_raw8bit_euckr_header()
    gen_raw8bit_attachment_filename()
    gen_raw8bit_contenttype_filename()
    gen_raw8bit_cd_ascii_ct_8bit_filename_priority()
    print("fixture 13종 생성 완료:", HERE)


if __name__ == "__main__":
    main()
