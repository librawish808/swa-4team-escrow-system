# QAS-03 · 가용성 및 장애 복구성 (내부/외부)

브랜치 `feature/qas-03-recovery` · 담당 A (외부 시스템 연동)
기준: 통합 DCD(`dcd-integrated.puml`), 시퀀스 다이어그램 SD-A01~A05, `qas-availability-recovery.md`

## 실행

다음 명령은 저장소 루트에서 실행한다. QAS-01과 같은 백엔드·프런트엔드를 쓴다.

```sh
cd backend
uv venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Windows(cmd)에서는 `.venv\Scripts\python`, `.venv\Scripts\uvicorn`으로 바꾼다.

다른 터미널에서 프런트엔드를 실행한다.

```sh
cd frontend
npm ci
npm run dev
```

[데모 화면](http://localhost:3000/qas-03) · [API 명세](http://127.0.0.1:8000/docs) (`QAS-03` 태그)

## 데모 흐름

1. **+ 데모 거래 시작**: 등록 → 참여 → 입금 확인 → 운송장 등록까지 끝난 100,000원 거래를 만든다. **+ 환불용 거래**는 입금 확인까지만 진행한다.
2. **외부 장애 (AV-01)**: 은행 상태를 `응답 유실`로 바꾸고 **구매확정**을 누른다.
   - 약 3초(타임아웃) 뒤 `처리 중 유지`로 응답한다. 화면은 '지급 확인 중'이고 완료로 표시하지 않는다.
   - 은행 이체 기록에는 실제 지급 1건이 보인다. 은행은 처리했지만 우리 시스템은 결과를 모르는 상황이다.
   - 장애 중에도 거래 생성·조회는 정상이다.
3. 은행을 `정상`으로 바꾸면 **복구 작업이 2초 안에** 같은 requestKey로 결과를 조회해 '거래 완료'로 확정한다. 은행 지급은 1건 그대로다.
   - `연결 실패`·`5xx`는 은행이 요청을 받지 못한 경우다. 복구 작업이 '기록 없음'을 확인하고 같은 requestKey로 다시 요청한다.
4. **내부 장애 (AV-02)**: **은행 호출 직전 종료** / **은행 처리 후 종료**는 이체 요청 순간 서버가 죽고 재시작되는 상황이다. 처리 중 상태가 DB에 남아 있어 재시작 직후 복구 작업이 확정한다.
5. **100건 자동 검증**: 별도 임시 DB에서 아래 측정 기준을 재현한다. 타임아웃은 50ms로 줄여 측정한다.

## 측정 기준과 검증

| QAS | 측정 기준 | 100건 검증 항목 | 단위 테스트 |
|---|---|---|---|
| AV-01 | 사용자 응답 10초 이내 | 최대 응답 시간 | `test_bank_down_payout_stays_processing_and_responds_fast` |
| AV-01 | 장애 시 완료로 표시하지 않음 | 장애 중 처리 중 유지 100/100 | `test_response_lost_is_unknown_not_failed_and_never_double_pays` |
| AV-01 | 장애 중 UC1·2·7 정상 | 다른 기능 10/10 | `test_other_features_work_while_bank_down` |
| AV-01 | 은행 복구 후 5분 이내 100% 확정 | 복구 후 확정, 복구 시간 | `test_100_transactions_recovered_after_bank_restored_without_duplicates` |
| AV-02 | 재시작 후 1분 이내 재처리 | 재시작 20건 확정, 재시작 후 복구 시간 | `InternalCrashRecoveryTest` |
| 공통 | 중복 지급·환불 0건 | 중복 / 누락 지급 0건, 중복 요청 차단 100건 | 위 테스트 전체 |

```sh
cd backend
.venv/bin/python -m unittest discover -s tests -v   # 단위 13개 + API 4개
.venv/bin/python -m app.qas03_demo                    # 100건 검증 (JSON 출력)
.venv/bin/python -m app.demo_availability             # 콘솔 시연
```

## Tactic → 코드

| Tactic | 위치 | 동작 |
|---|---|---|
| Timeout (장애 감지) | `external/bank_client.py` | `timeout_millis`(기본 3초) 안에 응답이 없으면 타임아웃 |
| 예외 감지·변환 | `external/bank_adapter.py` | 외부 예외를 B로 던지지 않고 `UNKNOWN` 결과로 반환 |
| Retry (조회 전용) | `BankAdapter._lookup`, `DeliveryAdapter` | 입금·결과·배송 조회만 재시도. 지급·환불 요청은 재시도하지 않음 |
| 외부 호출 전 상태 저장 | `transaction/service.py` | `start_payout()` → `save()` → `request_payout()` 순서 |
| 멱등키 (requestKey) | `Transaction.payout_request_key` | `PAYOUT-{거래ID}`. Mock 은행은 같은 키를 한 번만 처리 |
| 중복 요청 방지 | `policy/request_guard.py` | 점유 기록을 DB에 저장해 재시작 후에도 유지 |
| Degradation | `TransactionService` | 은행·택배 장애는 결과 코드로 반환하고 다른 기능은 계속 동작 |
| State Resynchronization | `recover_processing_transactions()`, `recovery/expiration_job.py` | 처리 중 거래를 `get_transfer_result(requestKey)`로 확인. 기록이 없으면 같은 키로 재요청. 서버 시작 직후 1회 + 주기 실행 |

## 코드 구성 (통합 DCD 대응)

| 위치 | DCD | 비고 |
|---|---|---|
| `backend/app/external/` | A. BankPort, DeliveryPort, Adapter·ApiClient·Mapper, 결과 타입 | Mock 은행·택배 포함, 장애 주입 가능 |
| `backend/app/transaction/` | B. Transaction, TransactionRepository, TransactionService | QAS-03 시연용 최소 구현 |
| `backend/app/policy/` | C. TransactionPolicy, RequestGuard | QAS-03 시연용 최소 구현 |
| `backend/app/recovery/` | ExpirationJob | 장애 복구. 기한 만료 판단은 통합 시 추가 |
| `backend/app/qas03_demo.py`, `qas03_api.py` | 데모 실행 상태, `/api/qas-03/...` | `main.py`에 라우터 한 줄로 연결 |
| `frontend/app/qas-03/` | 데모 화면 | 페이지 전용 CSS 모듈. QAS-01 화면과 같은 디자인 |

`transaction/`과 `policy/`는 QAS-02(B)·QAS-01(C) 구현과 통합할 때 인터페이스를 유지한 채 교체한다. 단, 교체 후에도 두 규칙은 유지해야 한다: **은행 호출 전에 처리 중 상태를 저장**하고, **`SUCCEEDED`일 때만 완료로 변경**한다.

## API (`/api/qas-03`)

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/state` | 은행 상태, 거래, 은행 이체 기록, 처리 기록, 복구 작업 상태 |
| POST | `/transactions` | 데모 거래 생성 (`stage`: `WAYBILL_REGISTERED` / `DEPOSIT_CONFIRMED`) |
| POST | `/bank-fault` | 은행 장애 주입 (`NONE` / `DOWN` / `SERVER_ERROR` / `RESPONSE_LOST`) |
| POST | `/transactions/{id}/confirm-purchase` | 구매확정 → 판매자 지급 |
| POST | `/transactions/{id}/cancel` | 거래 취소 → 구매자 환불 |
| POST | `/transactions/{id}/crash-restart` | 이체 요청 중 서버 종료 후 재시작 (`after_send`) |
| POST | `/recovery/run` | 복구 작업 즉시 실행 |
| POST | `/benchmark` | 100건 자동 검증 |
| POST | `/reset` | 데모 초기화 |

## 가정 · 범위

- 은행과 택배는 모의 구현이다. 실제 은행이 requestKey로 중복 이체를 막아 준다고 가정한다.
- Mock 은행은 거래 ID로 입금·지급 대상을 찾는다. 실제 연동에는 판매자 계좌·원입금 ID 전달이 필요하다.
- 은행 거절(`FAILED`) 후속 처리는 C 정책 결정 전이라 처리 중 상태를 유지하고 '담당자 확인 필요'를 표시한다.
- 데모 DB 기본 위치는 `backend/data/qas03.sqlite3`(`QAS03_DB_PATH`로 변경)다. Mock 은행은 메모리에 있으므로, uvicorn을 다시 실행하면 데모 DB도 비운다. 화면의 '서버 재시작'은 같은 프로세스 안에서 은행을 유지한 채 재시작을 재현한다.
- 운영 부하·다중 서버·실제 금융 연동의 가용성 보증은 이 로컬 데모 결과에 포함하지 않는다.
