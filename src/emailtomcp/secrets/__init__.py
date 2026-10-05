"""secrets 패키지 — 비밀번호/토큰 저장 (DESIGN.md §4.1, §8.1·§8.2).

`core.ports.SecretStore`의 운영 구현(`keyring_store.KeyringSecretStore`)만 둔다.
의존 규칙(§4.2): core에만 의존한다. PySide6, runtime, ui, mcp_server는 import하지 않는다.
"""
