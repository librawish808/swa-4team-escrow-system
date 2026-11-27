"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import styles from "./qas03.module.css";

type Fault = "NONE" | "DOWN" | "SERVER_ERROR" | "RESPONSE_LOST";
type Transaction = {
  id: string; amount: number; status: string; buyer_view: string; seller_view: string; version: number;
  payout_key: string; refund_key: string; bank_executed: number;
  can_confirm: boolean; can_cancel: boolean; processing: boolean;
};
type Report = { checked: number; completed: number; resent: number; still_processing: number; failed: number; skipped: number };
type State = {
  server_now: string; bank_fault: Fault; timeout_millis: number;
  job: { interval_seconds: number; last_run_at: string | null; last_report: Report | null; restarts: number };
  transactions: Transaction[];
  transfers: { request_key: string; kind: string; transaction_id: string; amount: number; status: string; attempts: number }[];
  events: { at: string; message: string; level: "info" | "warn" | "error" | "success"; transaction_id: string | null }[];
};
type ActionResult = { code: string; status: string; message: string; elapsed_ms: number };
type Benchmark = {
  runs: number; timeout_millis: number; held_processing: number; duplicates_blocked: number;
  other_features_ok: number; other_features_total: number; max_response_ms: number;
  recovered: number; resent: number; recovery_ms: number;
  crash_cases: number; crash_recovered: number; crash_resent: number; restart_recovery_ms: number;
  duplicate_payouts: number; missing_payouts: number; passed: boolean;
};

const cx = (...names: (string | false | null | undefined)[]) =>
  names.filter(Boolean).map((name) => styles[name as string] ?? name).join(" ");
const won = (value: number) => `${value.toLocaleString("ko-KR")}원`;
const time = (value: string) => new Date(value).toLocaleTimeString("ko-KR", {
  timeZone: "Asia/Seoul", hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3,
});
const ms = (value: number) => (value >= 1000 ? `${(value / 1000).toFixed(2)}초` : `${value.toFixed(1)}ms`);

const FAULTS: { value: Fault; label: string; hint: string }[] = [
  { value: "NONE", label: "정상", hint: "은행이 요청을 처리하고 바로 응답합니다." },
  { value: "DOWN", label: "연결 실패", hint: "요청이 은행에 도달하지 않습니다. 이체는 일어나지 않습니다." },
  { value: "SERVER_ERROR", label: "5xx 오류", hint: "은행 서버 오류로 요청이 처리되지 않습니다." },
  { value: "RESPONSE_LOST", label: "응답 유실", hint: "은행은 이체를 처리했지만 응답이 타임아웃 뒤에 도착합니다." },
];
const CODE_LABEL: Record<string, string> = {
  COMPLETED: "지급 완료", CANCELLED: "환불 완료", PROCESSING: "처리 중 유지",
  TRANSFER_FAILED: "은행 거절", DUPLICATE_REQUEST: "중복 요청 차단", REJECTED: "요청 거부",
};
async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api/qas-03${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(60_000)]) : AbortSignal.timeout(60_000),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(typeof data?.detail === "string" ? data.detail : `요청을 처리하지 못했습니다 (${response.status}).`);
  }
  return response.json();
}

export default function Qas03Page() {
  const [state, setState] = useState<State | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pollError, setPollError] = useState(false);
  const [lastResult, setLastResult] = useState<(ActionResult & { action: string }) | null>(null);
  const [benchmark, setBenchmark] = useState<Benchmark | null>(null);
  const actionLock = useRef(false);

  const transactions = state?.transactions ?? [];
  const transaction = transactions.find((item) => item.id === selectedId) ?? transactions[0];
  const fault = state?.bank_fault ?? "NONE";
  const connected = state !== null && !pollError;

  async function refresh(signal?: AbortSignal) {
    setState(await request<State>("/state", undefined, signal));
  }

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        await refresh(controller.signal);
        if (!controller.signal.aborted) setPollError(false);
      } catch { if (!controller.signal.aborted) setPollError(true); }
      finally { if (!controller.signal.aborted) timer = setTimeout(poll, 500); }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, []);

  async function act(name: string, action: () => Promise<void>) {
    if (actionLock.current) return;
    actionLock.current = true;
    setBusy(name); setError(null);
    try { await action(); await refresh(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "연결을 확인한 뒤 다시 시도해 주세요."); }
    finally { actionLock.current = false; setBusy(null); }
  }
  const create = (stage: "WAYBILL_REGISTERED" | "DEPOSIT_CONFIRMED") => act(`create-${stage}`, async () => {
    const { id } = await request<{ id: string }>("/transactions", { stage });
    setSelectedId(id); setLastResult(null);
  });
  const transfer = (kind: "confirm-purchase" | "cancel") => transaction && act(kind, async () => {
    const result = await request<ActionResult>(`/transactions/${transaction.id}/${kind}`, {});
    setLastResult({ ...result, action: kind === "confirm-purchase" ? "구매확정" : "거래 취소" });
  });
  const crash = (afterSend: boolean) => transaction && act(`crash-${afterSend}`, async () => {
    await request(`/transactions/${transaction.id}/crash-restart`, { after_send: afterSend });
    setLastResult(null);
  });

  const transfersOfTx = state?.transfers.filter((t) => t.transaction_id === transaction?.id) ?? [];
  const faultInfo = FAULTS.find((f) => f.value === fault) ?? FAULTS[0];
  const canCrash = !!transaction && (transaction.can_confirm || transaction.can_cancel);
  const disabled = !!busy || !connected;

  return (
    <div className={cx("page")}>
      <main className={cx("dashboard")}>
        <header className={cx("topbar")}>
          <Link href="/qas-03" className={cx("brand")}><span className={cx("brand-mark")} aria-hidden="true">E</span> Escrow <span className={cx("brand-divider")}>/</span><span className={cx("brand-subtitle")}>가용성·장애 복구 데모</span></Link>
          <span className={cx("connection", pollError && "offline")}><i />{pollError ? "백엔드 연결 확인 필요" : connected ? "실시간 연결됨" : "연결 중"}</span>
        </header>

        <section className={cx("intro")}>
          <div>
            <div className={cx("eyebrow")}>QAS-03 · AVAILABILITY &amp; RECOVERY</div>
            <h1>은행이 멈춰도,<br className={cx("mobile-break")} /> 돈은 정확히 한 번.</h1>
            <p>은행 장애·응답 유실·서버 종료가 일어나도 거래를 완료로 잘못 표시하지 않고, 복구되면 중복 없이 확정합니다.</p>
          </div>
          <div className={cx("intro-actions")}>
            <button className={cx("button", "secondary")} disabled={disabled} onClick={() => void create("DEPOSIT_CONFIRMED")}>{busy === "create-DEPOSIT_CONFIRMED" ? "준비 중…" : "+ 환불용 거래"}</button>
            <button className={cx("button", "primary")} disabled={disabled} onClick={() => void create("WAYBILL_REGISTERED")}>{busy === "create-WAYBILL_REGISTERED" ? "거래 준비 중…" : "+ 데모 거래 시작"}</button>
          </div>
        </section>

        {(error || pollError) && <div className={cx("alert")} role="alert">{error ?? "백엔드에 연결할 수 없습니다. 서버가 실행 중인지 확인해 주세요. 연결은 자동으로 다시 시도합니다."}</div>}

        <section className={cx("scenario-strip")} aria-label="시나리오 안내">
          <div><span className={cx("step-number")}>01</span><span>은행 장애 중 구매확정<br /><strong>지급 처리 중 유지</strong></span></div><span className={cx("step-arrow")} aria-hidden="true">→</span>
          <div><span className={cx("step-number")}>02</span><span>은행 정상화 · 서버 재시작<br /><strong>복구 작업이 결과 확인</strong></span></div><span className={cx("step-arrow")} aria-hidden="true">→</span>
          <div><span className={cx("step-number")}>03</span><span>같은 requestKey로 확정<br /><strong>은행 지급 정확히 1건</strong></span></div>
          <span className={cx("scenario-note")}>모의 은행 · 타임아웃 {(state?.timeout_millis ?? 3000) / 1000}초 · 복구 주기 {state?.job.interval_seconds ?? 2}초</span>
        </section>

        <div className={cx("workspace")}>
          <section className={cx("panel")}>
            <div className={cx("panel-heading")}><h3>외부 은행 상태</h3><span className={cx("pill", fault === "NONE" ? "success" : "warn")}>{fault === "NONE" ? "정상 운영" : "장애 주입 중"}</span></div>
            <div className={cx("segmented")} role="radiogroup" aria-label="은행 장애 종류">
              {FAULTS.map((f) => (
                <button key={f.value} role="radio" aria-checked={fault === f.value} className={cx("segment", fault === f.value && "active", f.value !== "NONE" && "danger")} disabled={disabled}
                  onClick={() => void act(`fault-${f.value}`, async () => { await request("/bank-fault", { fault: f.value }); })}>{f.label}</button>
              ))}
            </div>
            <p className={cx("muted", "fault-hint")}>{faultInfo.hint}</p>

            <div className={cx("divider")} />
            <div className={cx("panel-heading")}><h3>거래 조작</h3>{transaction && <span className={cx("pill", "neutral")}>거래 {transaction.id.slice(0, 8)}</span>}</div>
            {transaction ? <>
              <div className={cx("action-row")}>
                <button className={cx("button", "primary")} disabled={disabled || !transaction.can_confirm} onClick={() => void transfer("confirm-purchase")}>{busy === "confirm-purchase" ? "은행 응답 대기 중…" : "구매확정 (판매자 지급)"}</button>
                <button className={cx("button", "secondary")} disabled={disabled || !transaction.can_cancel} onClick={() => void transfer("cancel")}>{busy === "cancel" ? "은행 응답 대기 중…" : "거래 취소 (환불)"}</button>
              </div>
              {lastResult && <div className={cx("result-box")} role="status">
                <span className={cx("pill", lastResult.code === "COMPLETED" || lastResult.code === "CANCELLED" ? "success" : "warn")}>{CODE_LABEL[lastResult.code] ?? lastResult.code}</span>
                <span>{lastResult.action} 응답 <strong>{ms(lastResult.elapsed_ms)}</strong> <small>(기준 10초 이내)</small></span>
              </div>}
              <div className={cx("crash-box")}>
                <div><strong>서버 비정상 종료 재현</strong><p>이체를 요청하는 순간 서버가 죽고 바로 재시작합니다.</p></div>
                <div className={cx("crash-buttons")}>
                  <button className={cx("button", "secondary", "small")} disabled={disabled || !canCrash} onClick={() => void crash(false)}>{busy === "crash-false" ? "재시작 중…" : "은행 호출 직전 종료"}</button>
                  <button className={cx("button", "secondary", "small")} disabled={disabled || !canCrash} onClick={() => void crash(true)}>{busy === "crash-true" ? "재시작 중…" : "은행 처리 후 종료"}</button>
                </div>
              </div>
            </> : <p className={cx("empty-record")}>‘데모 거래 시작’으로 운송장 등록까지 끝난 거래를 만드세요.</p>}
          </section>

          <section className={cx("panel")}>
            <div className={cx("panel-heading")}>
              <h3>함께 보는 거래 상태</h3>
              {transactions.length > 1 ? <label className={cx("transaction-picker")}><span className={cx("sr-only")}>거래 선택</span><select value={transaction?.id} onChange={(event) => setSelectedId(event.target.value)} disabled={!!busy}>{transactions.map((item) => <option key={item.id} value={item.id}>거래 {item.id.slice(0, 8)} · {item.status}</option>)}</select></label>
                : <span className={cx("live-caption")}><i />0.5초마다 갱신</span>}
            </div>
            {transaction ? <>
              <div className={cx("party-grid")}>
                {[{ party: "구매자", label: transaction.buyer_view }, { party: "판매자", label: transaction.seller_view }].map((item, index) => (
                  <article className={cx("party-card")} key={item.party}>
                    <div className={cx("party-top")}><span className={cx("avatar", index === 1 && "seller")}>{index ? "S" : "B"}</span><span>{item.party} 화면</span></div>
                    <strong className={cx("return-state")}>{item.label}</strong>
                    <p>{transaction.processing ? "은행 처리 결과 확인 중" : transaction.status}</p>
                    <div className={cx("party-amount")}>결제 대금 <strong>{won(transaction.amount)}</strong></div>
                  </article>
                ))}
              </div>
              <div className={cx("hold-note", transaction.processing && "held")}>
                <span aria-hidden="true">{transaction.processing ? "◈" : "✓"}</span>
                <p>{transaction.processing
                  ? `은행 결과를 확인할 때까지 완료로 표시하지 않습니다. 복구 작업이 ${state?.job.interval_seconds ?? 2}초마다 같은 requestKey로 결과를 다시 확인합니다.`
                  : `은행 실제 이체 ${transaction.bank_executed}건 · 거래 버전 ${transaction.version}`}</p>
              </div>
              <div className={cx("job-row")}>
                <div><small>복구 작업 마지막 실행</small><strong>{state?.job.last_run_at ? time(state.job.last_run_at) : "—"}</strong></div>
                <div><small>서버 재시작</small><strong>{state?.job.restarts ?? 0}회</strong></div>
                <button className={cx("button", "secondary", "small")} disabled={disabled} onClick={() => void act("recover", async () => { await request("/recovery/run", {}); })}>{busy === "recover" ? "확인 중…" : "복구 작업 지금 실행"}</button>
              </div>
            </> : <p className={cx("empty-record")}>거래를 만들면 구매자·판매자 화면이 여기에 표시됩니다.</p>}
          </section>
        </div>

        <div className={cx("records-grid")}>
          <section className={cx("panel")}>
            <div className={cx("panel-heading")}><h3>처리 기록</h3><span className={cx("record-count")}>{state?.events.length ?? 0}건</span></div>
            <ol className={cx("timeline")}>
              {state?.events.length ? state.events.map((event, index) => (
                <li key={`${event.at}-${index}`} className={cx(event.transaction_id && event.transaction_id === transaction?.id && "current")}>
                  <span className={cx("timeline-dot", event.level)} /><div><p>{event.message}</p><time>{time(event.at)}</time></div>
                </li>
              )) : <li className={cx("empty-record")}>아직 처리 기록이 없습니다.</li>}
            </ol>
          </section>
          <section className={cx("panel")}>
            <div className={cx("panel-heading")}><h3>은행 이체 기록</h3><span className={cx("record-count")}>선택 거래 {transfersOfTx.length}건 · 전체 {state?.transfers.length ?? 0}건</span></div>
            <div className={cx("ledger")}>
              {state?.transfers.length ? state.transfers.map((item) => (
                <div className={cx("ledger-item", item.transaction_id === transaction?.id && "current")} key={item.request_key}>
                  <div><strong>{item.kind === "PAYOUT" ? "판매자 지급" : "구매자 환불"} · {won(item.amount)}</strong><small>{item.request_key}</small><small>은행에 도달한 요청 {item.attempts}회 → 실제 이체 1건</small></div>
                  <span className={cx("pill", item.status === "SUCCEEDED" ? "success" : "warn")}>{item.status === "SUCCEEDED" ? "이체 완료" : "거절"}</span>
                </div>
              )) : <p className={cx("empty-record")}>은행이 실제로 처리한 지급·환불이 여기에 기록됩니다.</p>}
            </div>
          </section>
        </div>

        <section className={cx("panel", "benchmark")}>
          <div className={cx("benchmark-intro")}>
            <div>
              <span className={cx("section-kicker")}>시나리오 자동 검증</span>
              <h2>같은 장애를 100번 재현합니다.</h2>
              <p>지급 100건을 은행 응답 유실·연결 실패 상태에서 요청 → 은행 복구 → 복구 작업 1회 실행. 서버 종료 20건은 재시작 후 복구.</p>
              <p>별도 임시 DB에서 실행하며 타임아웃은 50ms로 줄여 측정합니다. 화면의 데모 거래에는 영향이 없습니다.</p>
            </div>
            <button className={cx("button", "secondary")} disabled={disabled} onClick={() => void act("benchmark", async () => { setBenchmark(null); setBenchmark(await request<Benchmark>("/benchmark", {})); })}>{busy === "benchmark" ? "100건 검증 중…" : "100건 자동 검증 ↗"}</button>
          </div>
          {benchmark && <div className={cx("benchmark-result")} role="status">
            <span className={cx("benchmark-verdict", benchmark.passed ? "success" : "failure")}>{benchmark.passed ? "검증 통과" : "검증 실패"}</span>
            <div><small>장애 중 처리 중 유지</small><strong>{benchmark.held_processing} / {benchmark.runs}건</strong></div>
            <div><small>복구 후 확정 (재시작 포함)</small><strong>{benchmark.recovered + benchmark.crash_recovered} / {benchmark.runs + benchmark.crash_cases}건</strong></div>
            <div><small>중복 지급 / 누락 지급</small><strong>{benchmark.duplicate_payouts}건 / {benchmark.missing_payouts}건</strong></div>
            <div><small>최대 응답 · 복구 시간</small><strong>{ms(benchmark.max_response_ms)} · {ms(benchmark.recovery_ms)}</strong></div>
          </div>}
          {benchmark && <p className={cx("benchmark-detail")}>
            중복 요청 차단 {benchmark.duplicates_blocked}건 · 장애 중 다른 기능 {benchmark.other_features_ok}/{benchmark.other_features_total} 정상 · 같은 requestKey 재요청 {benchmark.resent + benchmark.crash_resent}건 · 재시작 후 복구 {ms(benchmark.restart_recovery_ms)}
          </p>}
        </section>

        <footer>QAS-03 가용성·장애 복구 검증용 데모입니다. 실제 금융 거래가 발생하지 않습니다. <span>시간 표시: Asia/Seoul</span></footer>
      </main>
    </div>
  );
}
