import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Escrow · QAS-01 신뢰성 데모",
  description: "반품 마감 직전 운송장 접수와 자동 지급 경합을 검증하는 에스크로 데모",
};

const themeInitScript = `try{var v=localStorage.getItem("color-scheme");if(v==="light"||v==="dark"){document.documentElement.dataset.theme=v;var m=document.querySelector('meta[name="color-scheme"]');if(m)m.content=v;}}catch(e){}`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html
      lang="ko"
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <head>
        <meta name="color-scheme" content="light dark" />
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
