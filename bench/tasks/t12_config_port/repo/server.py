from settings import DEFAULT_HOST, DEFAULT_PORT


def address(host: str | None = None, port: int | None = None) -> str:
    return f"{host or DEFAULT_HOST}:{port or DEFAULT_PORT}"
