"""메인 창 업데이트 배너 (DESIGN.md §11.1 배너 자리, §11.8 업데이트 UI 상태 표).

입력은 백엔드(`UiApi.get_update_status()`)가 돌려준 상태 dict(`UpdateStatus.to_dict()` +
`auto_check`/`stale`)다. 이 모듈은 `update` 패키지를 import하지 않는다(§4.2 — ui는 서비스
패키지를 모른다). 그래서 릴리스 링크 접두사 검사를 여기서도 한 번 더 한다(백엔드에서 이미
두 번 검증된 값이지만, 시스템 브라우저로 넘기기 직전의 마지막 방어선).

§11.8 상태별 표시
| 상태 | 표시 |
|---|---|
| 최신 | 배너 없음(설정 화면에만 "최신 버전입니다") |
| 새 버전 있음 | 비모달 배너 + 상태줄 배지. [나중에]는 그 버전에 대해 다시 띄우지 않는다 |
| 보안 업데이트 | 강조 배너. [나중에]로 닫으면 24시간 뒤 다시 표시 |
| floor 미달 | 고정 빨간 배너(닫기 버튼 없음) |
| 확인 불가 | 설정 화면에 사유. 7일 넘게 확인하지 못하면 배너 |
| 보안 경보 | 경보 다이얼로그 1회(메인 창이 SecurityAlert 이벤트로 띄움) + 상태줄 배지 |
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from PySide6.QtCore import QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QWidget

# update.manifest.RELEASES_URL_PREFIX와 같은 값(ui는 update를 import하지 않으므로 복제).
_RELEASE_HOST = "github.com"
_RELEASE_PATH_PREFIX = "/yuseungil-a11y/emailtomcp/releases/"

SECURITY_SNOOZE = timedelta(hours=24)

_QS_DISMISSED_VERSION = "update/dismissed_version"
_QS_SECURITY_DISMISSED_AT = "update/security_dismissed_at"

_LEVEL_STYLES = {
    # (배경, 글자) — 라이트/다크 모두에서 읽히도록 채도 있는 배경 + 흰 글자
    "floor": ("#f15347", "#ffffff"),  # destructive
    "security": ("#f7a443", "#1f2433"),  # favorite(주황) 강조
    "available": ("#5678ff", "#ffffff"),  # primary
    "stale": ("#8a90fc", "#ffffff"),  # primary-muted
}


def is_safe_release_url(url: object) -> bool:
    """https://github.com/yuseungil-a11y/emailtomcp/releases/ 아래의 평범한 URL인지."""
    if not isinstance(url, str) or not url:
        return False
    qurl = QUrl(url, QUrl.ParsingMode.StrictMode)
    if not qurl.isValid():
        return False
    path = qurl.path()
    return (
        qurl.scheme() == "https"
        and qurl.host() == _RELEASE_HOST
        and qurl.port() == -1
        and not qurl.userInfo()
        and not qurl.hasQuery()
        and not qurl.hasFragment()
        and path.startswith(_RELEASE_PATH_PREFIX)
        and ".." not in path
        and "%" not in url
        and url.startswith(f"https://{_RELEASE_HOST}{_RELEASE_PATH_PREFIX}")
    )


def describe_update_status(view: dict | None) -> str:
    """설정 화면의 '마지막 확인 결과' 문구."""
    if not view:
        return "아직 확인하지 않았습니다."
    kind = view.get("kind")
    latest = view.get("latest_version") or "?"
    reason = view.get("reason") or ""
    if kind == "never":
        return "아직 확인하지 않았습니다."
    if kind in ("up_to_date", "ignored"):
        text = "최신 버전입니다."
    elif kind == "available":
        text = f"새 버전 {latest}이(가) 있습니다."
        if view.get("security"):
            text += " (보안 업데이트)"
    elif kind == "below_floor":
        text = (
            f"필수 업데이트: 현재 버전이 최소 지원 버전 {view.get('floor_version') or '?'}보다 "
            f"낮습니다. 최신 버전은 {latest}입니다."
        )
    elif kind == "security_alert":
        text = (
            f"보안 경보: {reason} — 자동 확인을 정지했습니다. "
            "원인이 해소된 뒤 [지금 확인]으로 다시 확인할 수 있습니다."
        )
        return text
    elif kind == "unverifiable":
        text = f"확인 불가: {reason}" if reason else "확인 불가"
        if view.get("below_floor"):
            text += " (이전 확인 결과: 필수 업데이트 필요)"
        return text
    else:
        text = "알 수 없는 상태입니다."
    if reason and kind != "below_floor":
        text += f" {reason}"
    return text


@dataclass(frozen=True, slots=True)
class BannerSpec:
    level: str  # floor | security | available | stale
    text: str
    dismissible: bool
    has_link: bool
    version: str | None


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def compute_banner(
    view: dict | None,
    *,
    now: datetime,
    dismissed_version: str | None,
    security_dismissed_at: datetime | None,
    session_dismissed: set[str],
) -> BannerSpec | None:
    """상태 dict → 배너 사양(없으면 None). 순수 함수라 단위 테스트가 쉽다."""
    if not view:
        return None
    kind = view.get("kind")
    latest = view.get("latest_version")
    has_link = is_safe_release_url(view.get("release_page"))

    # 1) floor 미달: 확인 불가가 이어져도(below_floor 유지) 고정 배너
    if view.get("below_floor"):
        return BannerSpec(
            level="floor",
            text=(
                f"필수 업데이트: 현재 버전({view.get('current_version') or '?'})은 최소 지원 버전"
                f"({view.get('floor_version') or '?'})보다 낮습니다. 최신 버전"
                f"({latest or '?'})으로 업데이트하세요."
            ),
            dismissible=False,
            has_link=has_link,
            version=latest,
        )

    # 2) 새 버전(보안 업데이트 강조)
    if kind == "available" and latest:
        if view.get("security"):
            if security_dismissed_at is not None and now - security_dismissed_at < SECURITY_SNOOZE:
                return None
            return BannerSpec(
                level="security",
                text=f"보안 업데이트 {latest}이(가) 있습니다. 가능한 한 빨리 업데이트하세요.",
                dismissible=True,
                has_link=has_link,
                version=latest,
            )
        if dismissed_version == latest:
            return None
        return BannerSpec(
            level="available",
            text=f"새 버전 {latest}이(가) 있습니다.",
            dismissible=True,
            has_link=has_link,
            version=latest,
        )

    # 3) 7일 넘게 확인하지 못함
    if view.get("stale") and kind in ("unverifiable", "security_alert"):
        if "stale" in session_dismissed:
            return None
        return BannerSpec(
            level="stale",
            text=(
                "7일 넘게 업데이트를 확인하지 못했습니다. "
                "도구 > 설정 > 업데이트에서 사유를 확인하세요."
            ),
            dismissible=True,
            has_link=False,
            version=None,
        )
    return None


def status_badge(view: dict | None) -> tuple[str, str] | None:
    """상태줄 배지(텍스트, statusDot). 표시할 것이 없으면 None."""
    if not view:
        return None
    kind = view.get("kind")
    if kind == "security_alert":
        return "업데이트 ●보안 경보", "disconnected"
    if view.get("below_floor"):
        return "업데이트 ●필수", "disconnected"
    if kind == "available" and view.get("latest_version"):
        return f"업데이트 ●{view['latest_version']}", "connecting"
    return None


class UpdateBanner(QFrame):
    """§11.1 "(업데이트 배너: 새 버전 1.0.1 [릴리스 노트][다운로드 페이지 열기][나중에])"."""

    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("updateBanner")
        self._qsettings = QSettings("UTInfo", "EmailToMCP")
        self._session_dismissed: set[str] = set()
        self._view: dict | None = None
        self._spec: BannerSpec | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        self._label = QLabel("", self)
        self._label.setWordWrap(True)
        layout.addWidget(self._label, stretch=1)

        self.btn_notes = QPushButton("릴리스 노트", self)
        self.btn_notes.clicked.connect(self._on_notes)
        self.btn_download = QPushButton("다운로드 페이지 열기", self)
        self.btn_download.clicked.connect(self._on_download)
        self.btn_later = QPushButton("나중에", self)
        self.btn_later.clicked.connect(self._on_later)
        for btn in (self.btn_notes, self.btn_download, self.btn_later):
            layout.addWidget(btn)
        self.setVisible(False)

    @property
    def spec(self) -> BannerSpec | None:
        return self._spec

    @property
    def text(self) -> str:
        return self._label.text()

    def apply_status(self, view: dict | None, *, now: datetime | None = None) -> None:
        self._view = view
        security_at = _parse_dt(self._qsettings.value(_QS_SECURITY_DISMISSED_AT, "", type=str))
        raw_dismissed = self._qsettings.value(_QS_DISMISSED_VERSION, "", type=str)
        dismissed_version = (
            raw_dismissed if isinstance(raw_dismissed, str) and raw_dismissed else None
        )
        spec = compute_banner(
            view,
            now=now or datetime.now(UTC),
            dismissed_version=dismissed_version,
            security_dismissed_at=security_at,
            session_dismissed=self._session_dismissed,
        )
        self._spec = spec
        if spec is None:
            self.setVisible(False)
            return
        bg, fg = _LEVEL_STYLES[spec.level]
        self.setStyleSheet(
            f"QFrame#updateBanner {{ background-color: {bg}; border: none; }}"
            f"QFrame#updateBanner QLabel {{ color: {fg}; font-weight: 600; }}"
        )
        self._label.setText(spec.text)
        self.btn_notes.setVisible(spec.has_link or bool((view or {}).get("notes_summary")))
        self.btn_download.setVisible(spec.has_link)
        self.btn_later.setVisible(spec.dismissible)
        self.setVisible(True)

    def _release_url(self) -> str | None:
        url = (self._view or {}).get("release_page")
        return url if is_safe_release_url(url) else None

    def _on_notes(self) -> None:
        view = self._view or {}
        box = QMessageBox(self)
        box.setWindowTitle("릴리스 노트")
        # 서명된 값이어도 HTML로 해석하지 않는다.
        box.setTextFormat(Qt.TextFormat.PlainText)
        lines = [f"버전: {view.get('latest_version') or '?'}"]
        if view.get("released_at"):
            lines.append(f"릴리스: {view['released_at']}")
        if view.get("security"):
            lines.append("보안 업데이트입니다.")
        lines.append("")
        lines.append(view.get("notes_summary") or "(요약 없음)")
        box.setText("\n".join(lines))
        url = self._release_url()
        open_btn = (
            box.addButton("릴리스 페이지 열기", QMessageBox.ButtonRole.ActionRole) if url else None
        )
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if open_btn is not None and box.clickedButton() is open_btn:
            self._open_url(url)

    def _on_download(self) -> None:
        self._open_url(self._release_url())

    @staticmethod
    def _open_url(url: str | None) -> None:
        if url and is_safe_release_url(url):
            QDesktopServices.openUrl(QUrl(url))

    def _on_later(self) -> None:
        spec = self._spec
        if spec is None:
            return
        if spec.level == "security":
            self._qsettings.setValue(_QS_SECURITY_DISMISSED_AT, datetime.now(UTC).isoformat())
        elif spec.level == "available" and spec.version:
            self._qsettings.setValue(_QS_DISMISSED_VERSION, spec.version)
        else:
            self._session_dismissed.add(spec.level)
        self.setVisible(False)
        self.dismissed.emit()
