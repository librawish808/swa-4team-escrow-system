# QAS-02 거래 충돌 처리

`app/database.py`는 `.env`의 개별 연결 파라미터로 Supabase PostgreSQL에 연결합니다.
비밀번호를 저장한 `.env`는 커밋하지 않습니다.

## 처음 실행하기

backend 디렉터리에서 다음을 실행합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

`.env`에 Supabase Connect 화면의 Session pooler host, user, DB 비밀번호를 입력합니다.
따옴표·특수문자가 있는 비밀번호는 python-dotenv 형식에 맞게 작성합니다.
비밀번호를 URL에 조합하지 않고 개별 연결 파라미터로 전달합니다.

새 DB에서만 `migrations/001_transactions.sql`을 SQL Editor로 실행합니다.
공유 Supabase에는 이미 같은 테이블을 만들었으므로 다시 실행하지 않습니다.
이 SQL은 입금 완료부터 시작하는 최소 초안이며, 입금 전 상태와 실제 사용자 테이블은 후속 합의가 필요합니다.
RLS 정책은 아직 없으며 백엔드의 권한 있는 DB 연결을 사용합니다.

검증 결과와 남은 연동 범위는 [QAS-02 검증 기록](docs/qas02-validation.md)에 정리했습니다.

## 서비스 호출

```python
from app.external_ports import MockExternalPort
from app.transaction_service import register_waybill, cancel_transaction

port = MockExternalPort()  # 실제 은행/택배 호출 없음
result = register_waybill(transaction_id, expected_version, seller_id, "TEST123", port)
# 또는 cancel_transaction(transaction_id, expected_version, buyer_id, port)
```

호출자의 ID는 인증된 서버 정보여야 합니다. `app.main:app`은 인증 및 외부 Port를
의존성으로 연결하기 전까지 거래 요청에 503을 반환합니다. 충돌은 409로 매핑합니다.

## 로컬 API 데모

backend 디렉터리에서 실행합니다. 은행·택배는 Mock이지만 DB에는 실제 가상 거래가 저장됩니다.

```bash
.venv/bin/python -m uvicorn app.demo:app --host 127.0.0.1 --port 8000
```

브라우저에서 http://127.0.0.1:8000/docs 를 엽니다.

1. `POST /demo/transactions` → Try it out → Execute로 새 거래 ID를 받습니다.
2. `POST /transactions/{transaction_id}/waybill`에 해당 ID를 넣습니다.
3. 헤더 `X-Demo-Actor-Id`는 `10`, 본문은 `{"version":1,"waybill_no":"TEST123"}`로 실행합니다.
4. 200과 `WAYBILL_REGISTERED`, version 3을 확인합니다.
5. 같은 거래로 cancel에 헤더 `20`, 본문 `{"version":1}`을 보내면 409입니다.
6. 취소 성공을 보려면 새 데모 거래를 생성하고 먼저 cancel을 실행합니다.

외부 실패/불명확은 202와 `recovery_required=true`, DB 오류는 상세 정보를 숨긴 503입니다.
로컬 데모의 역할 선택 헤더는 인증 기능이 아닙니다. 팀 인증은 `current_actor` 의존성에
연결합니다. 데모 앱은 localhost에서만 실행하며 원격 클라이언트의 거래 요청을 차단합니다.

1. 현재 상태, 버전, 소유자를 단일 UPDATE로 검사하고 처리 상태를 커밋합니다.
2. 성공한 호출만 외부 Port를 실행합니다. Port에는 거래 금액과 멱등키를 전달합니다.
3. 외부 성공이면 선점한 상태와 버전이 그대로일 때만 최종 상태를 저장합니다.
4. 실패/불명확/예외이면 처리 중 상태를 유지하고 `recovery_required=True`를 반환합니다.
   일반 요청 재시도로 외부 호출을 반복하지 않습니다.

택배 성공은 `WAYBILL_REGISTERED`, 환불 성공은 `CANCELLED`입니다.
초기 version=1이면 선점 후 2, 최종 완료 후 3이 됩니다.

## QAS-03 연결 필요 사항

현재 단계는 자동 복구를 구현하지 않습니다. 프로세스 종료, 외부 성공 후 DB 저장 실패,
외부 실패/불명확 거래는 처리 중으로 남습니다. 외부 결과 조회 및 운영 복구가 필요합니다.
복구 담당자는 작업 기록(운송장 입력 포함)의 영속화와 동일 멱등키 사용 정책을 연결해야 합니다.
실제 Port도 멱등키를 지원해야 하며, 메모리 Mock의 중복 방지는 프로세스 재시작을 넘지 않습니다.

## 검증

backend 디렉터리에서:

```bash
.venv/bin/python -m app.check_db
.venv/bin/python -m unittest discover -s tests -v
```

테스트는 설정된 DB에 새 가상 거래를 만들고 해당 행만 삭제합니다. 기존 거래는 사용하지 않습니다.
기본적으로 전체 처리 동시 경쟁을 100회 실행합니다. `QAS02_ROUNDS=1`로 빠른 확인이 가능합니다.
각 경쟁에서 성공 1건, 충돌 1건, 외부 Mock 호출 1건과 DB 최종 결과 일치를 검사합니다.
권한/버전, 외부 실패/불명확/타임아웃 시 재실행 차단도 확인합니다.
