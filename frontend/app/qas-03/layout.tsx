import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "Escrow · QAS-03 가용성·장애 복구 데모",
  description: "은행 장애·응답 유실·서버 종료 상황에서 거래 상태를 보존하고 복구하는 에스크로 데모",
};

export default function Qas03Layout({ children }: { children: ReactNode }) {
  return children;
}
