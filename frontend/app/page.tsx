"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import Link from "next/link";

type Transaction = {
  id: string; amount: number; deadline: string; server_now: string; status: string;
  return_label: string; payment_held: boolean; version: number;
  receipts: { id: string; waybill_no: string; status: string; received_at: string; attempts: number }[];
  operations: { kind: string; status: string; amount: number; request_key: string }[];
  events: { at: string; message: string }[];
};
type Benchmark = {
  runs: number;
  payout_instructions: number;
  payout_transfers: number;
  hold_decisions: number;
  max_decision_ms?: number;
  p50_ms?: number;
  p95_ms?: number;
  p99_ms?: number;
  max_case_ms?: number;
  total_batch_ms?: number;
  invariants_passed?: boolean;
  invariant_violations?: string[];
  carrier_delay_seconds: number;
  passed: boolean;
};

const won = (value: number) => `${value.toLocaleString("ko-KR")}원`;
const time = (value: string) => new Date(value).toLocaleTimeString("ko-KR", {
  timeZone: "Asia/Seoul", hour12: false, hour: "2-digit", minute: "2-digit",
  second: "2-digit", fractionalSecondDigits: 3,
});
const statusLabel: Record<string, string> = {
  PENDING: "확인 중", VALID: "유효", INVALID: "거절", UNKNOWN: "재조회 대기",
  MANUAL_REVIEW: "담당자 확인", DELIVERED: "반품 도착", SUCCEEDED: "완료",
  PROCESSING: "처리 중", READY: "처리 대기", FAILED: "실패",
};

function applyColorScheme(value: "light" | "dark" | null) {
  const root = document.documentElement;
  if (value) root.dataset.theme = value;
  else delete root.dataset.theme;
  const meta = document.querySelector<HTMLMetaElement>('meta[name="color-scheme"]');
  if (meta) meta.content = value ?? "light dark";
}

function toggleColorScheme() {
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const systemScheme = systemDark ? "dark" : "light";
  let stored: string | null = null;
  try { stored = localStorage.getItem("color-scheme"); } catch { stored = null; }
  const current = stored === "light" || stored === "dark" ? stored : systemScheme;
  const target: "light" | "dark" = current === "dark" ? "light" : "dark";
  const nextStored = target === systemScheme ? null : target;
  try {
    if (nextStored) localStorage.setItem("color-scheme", nextStored);
    else localStorage.removeItem("color-scheme");
  } catch { /* ignore storage errors */ }
  applyColorScheme(nextStored);
}

async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store", signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(45_000)]) : AbortSignal.timeout(45_000),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(typeof data?.detail === "string" ? data.detail : `요청을 처리하지 못했습니다 (${response.status}).`);
  }
  return response.json();
}

export default function Home() {
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [waybill, setWaybill] = useState("VALID-001");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pollError, setPollError] = useState(false);
  const [connected, setConnected] = useState(false);
  const [benchmark, setBenchmark] = useState<Benchmark | null>(null);
  const actionLock = useRef(false);
  const transaction = transactions.find((item) => item.id === selectedId) ?? transactions[0];

  useEffect(() => {
    function onStorage(event: StorageEvent) {
      if (event.key !== "color-scheme") return;
      const value = event.newValue === "light" || event.newValue === "dark" ? event.newValue : null;
      applyColorScheme(value);
    }
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const items = await request<Transaction[]>("/transactions", undefined, controller.signal);
        if (controller.signal.aborted) return;
        setTransactions((previous) => items.map((item) => {
          const current = previous.find((entry) => entry.id === item.id);
          return current && current.version > item.version ? current : item;
        }));
        setConnected(true);
        setPollError(false);
      } catch { if (!controller.signal.aborted) setPollError(true); }
      finally { if (!controller.signal.aborted) timer = setTimeout(poll, 500); }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, []);

  function update(item: Transaction) {
    setTransactions((previous) => [item, ...previous.filter((entry) => entry.id !== item.id)]);
    setSelectedId(item.id);
  }
  async function act(name: string, action: () => Promise<void>) {
    if (actionLock.current) return;
    actionLock.current = true;
    setBusy(name); setError(null);
    try { await action(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "연결을 확인한 뒤 다시 시도해 주세요."); }
    finally { actionLock.current = false; setBusy(null); }
  }
  function submitWaybill(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!transaction || transaction.operations.length || !waybill.trim()) return;
    void act("receipt", async () => update(await request<Transaction>(`/transactions/${transaction.id}/return-waybills`, {
      actor_id: "buyer-demo", request_id: crypto.randomUUID(), waybill_no: waybill.trim(),
    })));
  }

  const remaining = transaction ? Math.max(0, Math.ceil((Date.parse(transaction.deadline) - Date.parse(transaction.server_now)) / 1000)) : 0;
  const deadlinePassed = transaction ? Date.parse(transaction.server_now) >= Date.parse(transaction.deadline) : false;
  const receiptClosed = deadlinePassed || !!transaction?.operations.length;
  const canArrive = transaction?.receipts.some((receipt) => receipt.status === "VALID") && !transaction?.operations.length;
  const holdMessage = transaction?.receipts.length
    ? "기한 내 접수한 반품의 확인과 배송이 끝날 때까지 지급을 보류합니다."
    : "반품 승인으로 판매자 지급을 보류하고 있습니다.";

  return (
    <main className="dashboard">
      <header className="topbar">
        <Link href="/" className="brand">
          <span className="brand-mark" aria-hidden="true">E</span> Escrow
          <span className="brand-divider">/</span>
          <span className="brand-subtitle">신뢰성 데모</span>
        </Link>
        <div className="topbar-right">
          <button type="button" className="theme-toggle" onClick={toggleColorScheme}>
            <span className="icon-moon" aria-hidden="false">
              <span aria-hidden="true">☾</span> 다크 모드
              <span className="sr-only">로 전환</span>
            </span>
            <span className="icon-sun" aria-hidden="false">
              <span aria-hidden="true">☀</span> 라이트 모드
              <span className="sr-only">로 전환</span>
            </span>
          </button>
          <span className={`connection ${pollError ? "offline" : ""}`}>
            <i />{pollError ? "백엔드 연결 확인 필요" : connected ? "실시간 연결됨" : "연결 중"}
          </span>
        </div>
      </header>

      <section className="intro">
        <div>
          <div className="eyebrow">QAS-01 · RELIABILITY &amp; INVARIANTS</div>
          <h1>마감 직전의 접수도,<br className="mobile-break" /> 안전하게.</h1>
          <p>반품 운송장 접수와 자동 지급이 겹쳐도, 먼저 접수한 반품과 DB·은행 원장 정합성을 보호합니다.</p>
        </div>
        <button
          className="button primary"
          disabled={!!busy || !connected || pollError}
          onClick={() => void act("create", async () => update(await request<Transaction>("/demo/transactions", { deadline_in_seconds: 15 })))}
        >
          {busy === "create" ? "거래 준비 중…" : "+ 데모 거래 시작"}
        </button>
      </section>

      {(error || pollError) && (
        <div className="alert" role="alert">
          {error ?? "백엔드에 연결할 수 없습니다. 서버가 실행 중인지 확인해 주세요. 연결은 자동으로 다시 시도합니다."}
        </div>
      )}

      <section className="scenario-strip" aria-label="시나리오 안내">
        <div><span className="step-number">01</span><span>마감 50ms 전<br /><strong>운송장 접수</strong></span></div>
        <span className="step-arrow" aria-hidden="true">→</span>
        <div><span className="step-number">02</span><span>자동 만료 작업<br /><strong>지급 보류 유지</strong></span></div>
        <span className="step-arrow" aria-hidden="true">→</span>
        <div><span className="step-number">03</span><span>택배 응답 3초 후<br /><strong>반품 진행 확인</strong></span></div>
        <span className="scenario-note">모의 택배 · 모의 은행 · 불변식 대조</span>
      </section>

      {transaction ? <>
        <section className="transaction-heading">
          <div>
            <span className="section-kicker">현재 거래</span>
            <h2>반품 승인된 거래 <span>{won(transaction.amount)}</span></h2>
          </div>
          <label className="transaction-picker">
            <span className="sr-only">거래 선택</span>
            <select value={transaction.id} onChange={(event) => setSelectedId(event.target.value)} disabled={!!busy}>
              {transactions.map((item) => <option key={item.id} value={item.id}>거래 {item.id.slice(0, 8)}</option>)}
            </select>
          </label>
        </section>

        <div className="workspace">
          <section className="panel interaction-panel">
            <div className="panel-heading">
              <h3>반품 운송장 접수</h3>
              <span className={`pill ${deadlinePassed ? "neutral" : "blue"}`}>
                {deadlinePassed ? "등록 마감" : `${remaining}초 남음`}
              </span>
            </div>
            <p className="muted">반품 안내 최초 게시 후 72시간이 마감 기준입니다.<br />이 거래는 남은 시간을 15초로 설정한 데모입니다.</p>
            <div className="deadline">
              <span>운송장 등록 마감 · KST</span>
              <strong>{time(transaction.deadline)}</strong>
              <small>서버 시각 {time(transaction.server_now)}</small>
            </div>
            <form onSubmit={submitWaybill}>
              <label htmlFor="waybill">반품 운송장 번호</label>
              <div className="input-group">
                <input
                  id="waybill"
                  value={waybill}
                  onChange={(event) => setWaybill(event.target.value)}
                  placeholder="VALID-001"
                  required
                  maxLength={100}
                  disabled={!!busy || receiptClosed}
                  autoComplete="off"
                />
                <button className="button primary" disabled={!!busy || receiptClosed || !waybill.trim() || pollError}>
                  {busy === "receipt" ? "접수 중…" : "운송장 접수"}
                </button>
              </div>
              <p className="field-hint">VALID: 정상 · INVALID: 무효 · WRONG: 수취인 불일치 · TIMEOUT: 응답 없음</p>
            </form>
            <div className="arrival-row">
              <div>
                <strong>판매자에게 반품이 도착했나요?</strong>
                <p>택배사 도착 확인을 재현하면 환불을 요청합니다.</p>
              </div>
              <button
                className="button secondary"
                disabled={!!busy || !canArrive || pollError}
                onClick={() => void act("arrive", async () => update(await request<Transaction>(`/demo/transactions/${transaction.id}/arrive`, {})))}
              >
                {busy === "arrive" ? "확인 중…" : "반품 도착 재현"}
              </button>
            </div>
          </section>

          <section className="panel parties-panel">
            <div className="panel-heading">
              <h3>함께 보는 거래 상태</h3>
              <span className="live-caption"><i />0.5초마다 갱신</span>
            </div>
            <div className="party-grid">
              {["구매자", "판매자"].map((party, index) => (
                <article className="party-card" key={party}>
                  <div className="party-top">
                    <span className={`avatar ${index ? "seller" : ""}`}>{index ? "S" : "B"}</span>
                    <span>{party} 화면</span>
                  </div>
                  <strong className="return-state">{transaction.return_label}</strong>
                  <p>{transaction.payment_held ? "결제 대금 지급 보류 중" : transaction.operations.some((operation) => operation.kind === "REFUND") ? "구매자 환불 처리" : "판매자 지급 처리"}</p>
                  <div className="party-amount">결제 대금 <strong>{won(transaction.amount)}</strong></div>
                </article>
              ))}
            </div>
            <div className={`hold-note ${transaction.payment_held ? "held" : ""}`}>
              <span aria-hidden="true">{transaction.payment_held ? "◈" : "✓"}</span>
              <p>{transaction.payment_held ? holdMessage : "지급·환불 기록에서 처리 결과를 확인할 수 있습니다."}</p>
            </div>
          </section>
        </div>

        <div className="records-grid">
          <section className="panel">
            <div className="panel-heading">
              <h3>접수 및 처리 기록</h3>
              <span className="record-count">{transaction.events.length}건</span>
            </div>
            <ol className="timeline">
              {transaction.events.length ? transaction.events.map((event, index) => (
                <li key={`${event.at}-${index}`}>
                  <span className="timeline-dot" />
                  <div><p>{event.message}</p><time>{time(event.at)}</time></div>
                </li>
              )) : <li className="empty-record">아직 처리 기록이 없습니다.</li>}
            </ol>
          </section>

          <section className="panel">
            <div className="panel-heading">
              <h3>운송장 · 자금 기록</h3>
              <span className="record-count">버전 {transaction.version}</span>
            </div>
            <div className="ledger">
              {transaction.receipts.map((receipt) => (
                <div className="ledger-item" key={receipt.id}>
                  <div>
                    <strong>{receipt.waybill_no}</strong>
                    <small>접수 {time(receipt.received_at)} · 조회 {receipt.attempts}회</small>
                  </div>
                  <span className="pill neutral">{statusLabel[receipt.status] ?? receipt.status}</span>
                </div>
              ))}
              {transaction.operations.map((operation) => (
                <div className="ledger-item" key={operation.request_key}>
                  <div>
                    <strong>{operation.kind === "PAYOUT" ? "판매자 지급" : "구매자 환불"} · {won(operation.amount)}</strong>
                    <small>{operation.request_key}</small>
                  </div>
                  <span className="pill blue">{operation.status === "PENDING" ? "처리 대기" : statusLabel[operation.status] ?? operation.status}</span>
                </div>
              ))}
              {!transaction.receipts.length && !transaction.operations.length && (
                <p className="empty-record">운송장 접수와 지급·환불 지시가 여기에 기록됩니다.</p>
              )}
            </div>
          </section>
        </div>
      </> : (
        <section className="panel empty-state">
          <span className="empty-icon" aria-hidden="true">↗</span>
          <h2>마감 경합을 직접 확인하세요</h2>
          <p>‘데모 거래 시작’을 누르고 15초 안에 운송장을 접수하세요.<br />택배사가 응답하기 전에 마감되어도 지급 보류가 유지됩니다.</p>
        </section>
      )}

      <section className="benchmark panel">
        <div className="benchmark-intro">
          <div>
            <span className="section-kicker">시나리오 자동 검증</span>
            <h2>마감 경합 · Tail Latency · 불변식을 자동 검증합니다.</h2>
            <p>마감 50ms 전 접수 재현 · 모의 택배 3초 지연 · 개별 결정 p95 ≤ 50ms / p99 ≤ 100ms / max ≤ 200ms · DB↔은행 원장 모순 0건</p>
          </div>
          <div className="benchmark-actions">
            <button
              className="button secondary"
              disabled={!!busy || !connected || pollError}
              onClick={() => void act("benchmark-100", async () => {
                setBenchmark(null);
                setBenchmark(await request<Benchmark>("/demo/qas-01", {}));
              })}
            >
              {busy === "benchmark-100" ? "100건 검증 중…" : "100건 기본 검증 ↗"}
            </button>
            <button
              className="button primary"
              disabled={!!busy || !connected || pollError}
              onClick={() => void act("benchmark-1000", async () => {
                setBenchmark(null);
                setBenchmark(await request<Benchmark>("/demo/qas-01/stress", {}));
              })}
            >
              {busy === "benchmark-1000" ? "1,000건 검증 중…" : "1,000건 스트레스 검증 ↗"}
            </button>
          </div>
        </div>

        {benchmark && (
          <div className="benchmark-result" role="status">
            <span className={`benchmark-verdict ${benchmark.passed ? "success" : "failure"}`}>
              {benchmark.passed ? `검증 통과 (${benchmark.runs.toLocaleString("ko-KR")}건)` : `검증 실패 (${benchmark.runs.toLocaleString("ko-KR")}건)`}
            </span>
            <div>
              <small>지급 지시 / 실제 지급</small>
              <strong>{benchmark.payout_instructions}건 / {benchmark.payout_transfers}건</strong>
              <span className="benchmark-sub">목표: 각 0건</span>
            </div>
            <div>
              <small>지급 보류 결정</small>
              <strong>{benchmark.hold_decisions.toLocaleString("ko-KR")} / {benchmark.runs.toLocaleString("ko-KR")}건</strong>
              <span className="benchmark-sub">100% 보류 유지</span>
            </div>
            <div>
              <small>Tail Latency (p95 / p99 / max)</small>
              <strong>
                {(benchmark.p95_ms ?? 0).toFixed(2)} / {(benchmark.p99_ms ?? 0).toFixed(2)} / {(benchmark.max_case_ms ?? benchmark.max_decision_ms ?? 0).toFixed(2)}ms
              </strong>
              <span className="benchmark-sub">
                {benchmark.max_decision_ms !== undefined
                  ? `공통 마감 누적 ${benchmark.max_decision_ms.toFixed(1)}ms`
                  : `전체 배치 ${(benchmark.total_batch_ms ?? 0).toFixed(1)}ms`}
              </span>
            </div>
            <div>
              <small>관계적 불변식 (DB↔은행)</small>
              <strong>
                {benchmark.invariants_passed === false
                  ? `위반 ${(benchmark.invariant_violations ?? []).length}건`
                  : "모순 0건 ✓"}
              </strong>
              <span className="benchmark-sub">원금 배타 · 인과 · 원장 일치</span>
            </div>
          </div>
        )}
      </section>

      <footer>
        QAS-01 신뢰성 검증용 데모입니다. 실제 택배 조회나 금융 거래가 발생하지 않습니다.
        <span>시간 표시: Asia/Seoul</span>
      </footer>
    </main>
  );
}
