# C 담당 영역 문서 정렬 검수 및 보완 기록

기준 산출물 (`docs/artifacts/`):
- `docs/artifacts/dcd-integrated.puml`
- `docs/artifacts/architecture-decision.md`
- `docs/artifacts/QAS-01md.md`
- `docs/artifacts/qas-availability-recovery.md`
- `docs/artifacts/sd-a01-deposit-lookup.puml` ~ `sd-a05-shipment-lookup.puml`

대상 브랜치: `feature/qas-01-reliability`  
검수 및 보완 일자: 2026-11-26

---

## 1. 1차 검수 지적 사항 및 보완 내역

| # | 구분 | 검수 지적 사항 | 보완 내역 | 상태 |
| --- | --- | --- | --- | --- |
| 1-1 | 계층 책임 누수 | `SqlTransactionRepository.accept_waybill()`이 구매자 권한, 중복 요청 ID 충돌, 기한 경과, 이미 결정된 지시 여부를 직접 검증 | `PolicyController.evaluate(REGISTER_RETURN_WAYBILL)`로 위임 (`backend/app/policy.py`, `backend/app/repository.py`) | **수정 완료** |
| 1-2 | 계층 책임 누수 | `SqlTransactionRepository.finish_receipt()`가 택배 결과 및 재시도 한도(`VALID`/`INVALID`/`DELIVERED`/`UNKNOWN`/`MANUAL_REVIEW`)를 직접 판정 | `ReturnPolicy.evaluate_shipment_result()` / `PolicyController.evaluate(EVALUATE_RETURN_SHIPMENT)`로 이동 | **수정 완료** |
| 1-3 | 계층 책임 누수 | `SqlTransactionRepository.mark_mock_arrived()`가 `VALID` 운송장 존재 여부를 직접 검사 | `PolicyController.evaluate(MARK_RETURN_ARRIVED)`로 위임 | **수정 완료** |
| 1-4 | 계층 책임 누수 | `TransactionService.get_transaction_status()`가 조회 참여자 권한을 직접 검사 | `PolicyController.validate_read()` (`TransactionPolicy`)로 위임 | **수정 완료** |
| 1-5 | 시그니처 불일치 | `TransactionPolicy`, `PolicyRequest`/`PolicyContext`/`PolicyDecision`, `DeadlinePolicy.isDue()` 및 `period` 인자, `ReturnPolicy.evaluateIssue/evaluateReturn` 미구현 | 통합 DCD 시그니처와 VO/DTO를 `backend/app/domain.py`, `backend/app/policy.py`에 정의·구현하고 `now >= deadline` 직접 비교를 `DeadlinePolicy.is_due()`로 통일 | **수정 완료** |
| 1-6 | 미구현 정책 4종 | `DepositPolicy`, `FulfillmentPolicy`, `CancellationPolicy`, `FundsPolicy` 미구현 | 4개 순수 정책 클래스를 `backend/app/policy.py`에 구현하고 단위 테스트(`backend/tests/test_policy.py`) 추가 | **수정 완료** |
| 1-7 | 자금 모델·포트 계약 | `Deposit`, `DepositAllocation`, `FundsOperation`, `TransferAttempt`, `TransferResult`/`TransferStatus`/`LookupStatus` 및 `BankPort.get_transfer_result()` 누락 | `backend/app/domain.py`, `backend/app/adapters.py`, `backend/app/service.py`에 통합 DCD 계약 반영 및 `UNKNOWN`/`FAILED`/`NOT_FOUND` 복구 경로 연결 | **수정 완료** |
| 1-8 | 접수 시각 해석 충돌 | `architecture-decision.md` §5의 "서버 최초 접수 시각"과 로컬 구현의 DB 쓰기 트랜잭션 진입 시각 기준 차이 | 단일 노드 선형화 지점(DB 쓰기 트랜잭션 진입 시점)에서 획득한 시각을 `PolicyRequest.received_at`으로 C에 전달하도록 구조화하고, 다중 인스턴스 환경과의 차이를 `docs/QAS-01.md`·`docs/qas-01-dcd.puml`에 명시 | **문서·구조 정렬 완료** |

---

## 2. 2차 재검수 발견 사항 및 보완 내역

| # | 구분 | 재검수 발견 사항 | 보완 내역 | 상태 |
| --- | --- | --- | --- | --- |
| 2-1 | AV-01 / SD-A02 버그 | `TransactionService.dispatch_transfers()`에서 은행 호출 중 `TimeoutError`/`OSError` 발생 시 `UNKNOWN`으로 전환하지 못해 `PENDING`으로 남아 다음 주기에 원요청을 재전송할 위험 | `_submit_transfer()`·`_lookup_transfer()`에 5초 타임아웃과 `(TimeoutError, OSError) -> TransferStatus.UNKNOWN` 변환 추가. `REPLAY_TRANSFER_SAME_KEY` 재전송 응답도 `FundsPolicy`를 다시 거치도록 통일 | **수정 완료** |
| 2-2 | SD-A04 점유·버전 검사 | `receipts`와 달리 `operations`에 작업 점유(`tryClaimWork`)와 `save_transfer_result(..., expected_version)` 버전 검사가 누락됨 | `operations` 테이블에 `lease_token`/`lease_until` 컬럼(기존 DB 마이그레이션 포함), `claim_operation()`, `save_transfer_result(..., lease_token, expected_version)` 구현 | **수정 완료** |
| 2-3 | C 엣지케이스 | `DepositPolicy.evaluate_deposit()`에서 `received_amount <= 0` 검사가 상태/기한 검사보다 뒤에 있어 취소 거래의 0원 입금에 0원 `UNAPPLIED` 배분이 생성됨 | `received_amount <= 0` 거절(`DecisionType.REJECT`, `400`)을 최상단으로 이동 | **수정 완료** |
| 2-4 | C 순수성 | `PolicyController.validate_*` 5개 메서드가 내부에서 `utc_now()`를 직접 호출해 시계 주입 테스트가 어려움 | 선택적 `now: datetime | None = None` 파라미터를 추가해 결정론적 테스트 지원 | **수정 완료** |
| 2-5 | 모듈 의존성 | `domain.py` 하단에서 `policy.py`를 re-export하는 순환 import(`# noqa: E402`) 존재 | 순환 import를 제거하고 `repository.py`, `service.py`, `test_policy.py`가 `policy.py`에서 직접 import하도록 정리 | **수정 완료** |

---

## 3. 미수정 / 팀 통합 단계 과제 (현재 QAS-01 브랜치 범위 외)

아래 항목은 현재 `feature/qas-01-reliability` 브랜치의 의도된 범위(로컬 QAS-01 신뢰성 데모 및 C 정책 계층 정렬) 밖이며, 팀 전체(A·B·C) 통합 단계에서 진행해야 합니다.

| # | 항목 | 근거 문서 | 현재 상태 및 향후 조치 |
| --- | --- | --- | --- |
| 3-1 | 전체 7개 UC API 및 `TransactionService` 연결 (`createTransaction`~`getTransactionStatus`) | `dcd-integrated.puml`, `architecture-decision.md` §6 | C의 6개 정책 클래스와 `TransactionPolicy` 5개 메서드는 구현·단위 검증 완료. B(`TransactionService`)·API(`main.py`) 통합 시 연결 필요 |
| 3-2 | DB 엔진 전환 및 자금 4개 테이블 분리 | `architecture-decision.md` §4, `dcd-integrated.puml` | 현재 로컬 SQLite 단일 `operations` 테이블 사용. 통합 시 PostgreSQL로 전환하고 `deposits`, `deposit_allocations`, `funds_operations`, `transfer_attempts` 테이블을 분리해 `EXCESS`·`UNAPPLIED` 영속 복구 경로 연결 필요 |
| 3-3 | 다중 인스턴스 분산 접수 시각 순서·대기 규칙 | `architecture-decision.md` §5 | 단일 프로세스에서는 `BEGIN IMMEDIATE` 진입 시각으로 선형화. 다중 인스턴스 배포 시 서버 최초 접수 시각과 저장 완료 시각을 분리하는 거래별 순서·대기 규칙 및 무중단 순차 배포 검증 필요 |
| 3-4 | V01~V34 통합 검증 사례 및 실제 은행·택배 어댑터 연동 | `architecture-decision.md` §7, `qas-availability-recovery.md` §5 | 현재 모의 어댑터(`MockBankPort`, `MockDeliveryPort`) 기반. A 어댑터 구현체와 팀 합의 수치(타임아웃·재시도·복구 목표) 확정 후 전체 통합 시험 필요 |

---

## 4. 강화된 성능 · 정합성 · 장애 복구 기준 (3축 추가 적용)

| # | 축 | 구현 내역 | 검증 결과 |
| --- | --- | --- | --- |
| 4-1 | 성능 강화 (1,000건 Tail Latency + Bulkhead) | `SqlTransactionRepository` WAL `synchronous=NORMAL` 적용, `benchmark.py`에 `percentile()` 및 `run_qas01_stress(runs=1000)` 추가, `POST /api/demo/qas-01/stress` 엔드포인트 추가 | 1,000건 경합 `p50=1.472ms`, `p95=1.871ms`, `p99=2.386ms`, `max=4.226ms`, 지급 0건 / 외부 100% 장애 중 독립 API `p99 ≤ 50ms` |
| 4-2 | 관계적 정합성 검증기 (`InvariantChecker`) | `backend/app/invariants.py`에 원금 배타성·인과 순서·DB↔은행 원장 대칭차집합 `∅`·단조 버전·자금 보존 법칙(`verify_money_conservation`) 자동 검증기 구현 및 벤치마크 연동 | 100건·1,000건 벤치마크 및 Chaos 테스트 위반 **0건** (부정 테스트로 위반 탐지 확인) |
| 4-3 | 장애 주입(Chaos) & UI-DB 정합성 | `backend/tests/test_chaos_and_invariants.py`에 4개 크래시 지점 복구(RPO=0, RTO ≤ 5초), 좀비 워커 Fencing 차단, 전면 외부 장애 Fail-Safe, UI↔DB 단조 읽기·양측 대칭성 테스트 추가 | 전체 통과 (`34/34` tests OK) |

---

## 5. 최종 검증 요약

- 백엔드 단위·통합·Chaos 테스트: `backend/.venv/bin/python -m unittest discover -s tests -v` — **34 / 34 통과**
- 100건 QAS-01 벤치마크: `backend/.venv/bin/python -m app.benchmark` — **통과** (`max_decision_ms=127.878ms ≤ 200ms`, `p99=1.822ms`, `invariants_passed=True`)
- 1,000건 스트레스 벤치마크: `run_qas01_stress(runs=1000)` — **통과** (`p95=1.871ms`, `p99=2.386ms`, `max_case_ms=4.226ms`, `invariants_passed=True`)
- 프런트엔드 정적 검증: `npm run lint` · `npx tsc --noEmit` — **오류 0건 통과**

