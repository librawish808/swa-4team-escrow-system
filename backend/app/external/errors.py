"""외부 호출 실패 예외. Adapter 밖(B)으로는 나가지 않고 UNKNOWN 결과로 바뀐다."""


class ExternalCallError(Exception):
    """외부 시스템 호출 실패의 공통 부모."""


class ExternalTimeoutError(ExternalCallError):
    """timeoutMillis 안에 응답이 오지 않음. 요청이 처리됐을 수도 있다."""


class ExternalConnectionError(ExternalCallError):
    """연결 실패. 요청이 상대에게 도달하지 않았다."""


class ExternalServerError(ExternalCallError):
    """상대 시스템의 5xx 오류."""
