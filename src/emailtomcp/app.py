"""앱 진입점(GUI 구동).

담당 범위: 단일 인스턴스 보장, 로깅 설정, DB 마이그레이션, Backend 시작, QSS 로드,
MainWindow 생성, 종료 시퀀스. 의존성 와이어링(서비스 조립)은 여기서 한다
(소크라테스 §1.3 "의존성 주입(와이어링)은 app.py에서 처리한다").
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from importlib import resources

from PySide6.QtCore import QLockFile
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from emailtomcp._version import __version__
from emailtomcp.config import paths
from emailtomcp.core.events import EventBus
from emailtomcp.runtime.backend import Backend
from emailtomcp.runtime.qt_bridge import QtBridge
from emailtomcp.storage.db import Database
from emailtomcp.ui.main_window import MainWindow

logger = logging.getLogger(__name__)

SINGLE_INSTANCE_KEY = "EmailToMCP-single-instance"
THEME_PACKAGE = "emailtomcp.ui.resources.themes"


class _SecretMaskingFilter(logging.Filter):
    """비밀번호/토큰이 로그에 그대로 남지 않도록 막는 2차 안전망.

    1차 책임은 각 모듈이 로그 메시지를 만들 때 직접 마스킹하는 것이고, 이 필터는
    혹시 새어 들어온 `password=...`, `token=...`, `Bearer <값>` 패턴을 한 번 더 가린다.
    """

    _PATTERNS = (
        (re.compile(r"(?i)(password\s*=\s*)(\S+)"), r"\1***"),
        (re.compile(r"(?i)(token\s*=\s*)(\S+)"), r"\1***"),
        (re.compile(r"(?i)(Bearer\s+)(\S+)"), r"\1***"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        masked = msg
        for pattern, repl in self._PATTERNS:
            masked = pattern.sub(repl, masked)
        if masked != msg:
            record.msg = masked
            record.args = ()
        return True


def setup_logging() -> None:
    """stdlib logging만 쓴다. RotatingFileHandler 하나를 여기서만 설정한다(소크라테스 §5.2)."""
    log_path = paths.log_dir() / "emailtomcp.log"
    root_logger = logging.getLogger("emailtomcp")
    root_logger.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_SecretMaskingFilter())
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(_SecretMaskingFilter())
    root_logger.addHandler(console_handler)


def _load_stylesheet(theme: str = "light") -> str:
    """패키지 리소스에서 QSS를 읽는다.

    importlib.resources를 써서 PyInstaller onedir 번들 안에서도 동작한다.
    """
    filename = f"{theme}.qss"
    try:
        return resources.files(THEME_PACKAGE).joinpath(filename).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        logger.warning("QSS 테마를 찾을 수 없습니다: %s/%s", THEME_PACKAGE, filename)
        return ""


class SingleInstanceGuard:
    """QLockFile + QLocalServer로 단일 인스턴스를 보장한다(DESIGN.md §2.a).

    두 번째 실행은 `try_acquire`가 False를 반환하기 전에, 이미 떠 있는 인스턴스로
    활성화 요청(짧은 바이트열)을 보낸다. 기존 인스턴스는 `on_activate_requested`로 이를 받는다.
    """

    def __init__(self, key: str) -> None:
        self._key = key
        self._lock_file = QLockFile(str(paths.lock_file_path()))
        self._server: QLocalServer | None = None

    def try_acquire(self, on_activate_requested) -> bool:  # noqa: ANN001 — Qt 슬롯 콜백
        if self._lock_file.tryLock(100):
            QLocalServer.removeServer(self._key)
            self._server = QLocalServer()
            self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
            self._server.listen(self._key)
            self._server.newConnection.connect(on_activate_requested)
            return True

        # 이미 떠 있는 인스턴스에 활성화 요청을 보낸다.
        socket = QLocalSocket()
        socket.connectToServer(self._key)
        if socket.waitForConnected(200):
            socket.write(b"activate")
            socket.flush()
            socket.waitForBytesWritten(200)
            socket.disconnectFromServer()
        return False

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        self._lock_file.unlock()


def run_app(mcp_port: int = 0) -> int:
    """GUI 애플리케이션을 구동한다. `mcp_port`는 P2(MCP 서버)에서 실제로 쓰인다."""
    setup_logging()
    logger.info("EmailToMCP %s 시작", __version__)

    app = QApplication(sys.argv)
    app.setApplicationName("EmailToMCP")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("UTInfo")

    stylesheet = _load_stylesheet("light")
    if stylesheet:
        app.setStyleSheet(stylesheet)

    guard = SingleInstanceGuard(SINGLE_INSTANCE_KEY)
    window_holder: dict[str, MainWindow] = {}

    def _activate_existing(*_args: object) -> None:
        window = window_holder.get("window")
        if window is not None:
            window.show()
            window.raise_()
            window.activateWindow()

    if not guard.try_acquire(_activate_existing):
        logger.info("이미 실행 중인 인스턴스가 있어 활성화 요청만 보내고 종료합니다")
        return 0

    db = Database(paths.db_path())
    db.start()
    applied_version = db.migrate()
    logger.info("DB 마이그레이션 완료: user_version=%s", applied_version)

    event_bus = EventBus()
    backend = Backend()
    backend.start()
    bridge = QtBridge(backend, event_bus)

    window = MainWindow(version=__version__, event_bus=event_bus, bridge=bridge)
    window_holder["window"] = window
    window.show()

    def _shutdown() -> None:
        logger.info("종료 시퀀스 시작")
        # 1. 새 작업 수락 중단, 2. 백엔드 작업 정리
        # 3. 루프 stop, 4. 스레드 join (Backend.stop 내부에서 순서대로 처리)
        backend.stop()
        # 5. writer 종료
        db.stop()
        # 6. 락 해제
        guard.release()
        logger.info("종료 시퀀스 완료")

    app.aboutToQuit.connect(_shutdown)

    return app.exec()
