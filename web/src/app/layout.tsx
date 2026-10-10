import type { Metadata } from "next";
import { Fira_Code, Fira_Sans } from "next/font/google";
import { AppShell } from "@/components/shell/AppShell";
import { themeInitScript } from "@/lib/theme";
import "./globals.css";

const firaSans = Fira_Sans({
  variable: "--font-fira-sans",
  weight: ["400", "500", "600", "700"],
  subsets: ["latin"],
});

// Mono for every figure on the dashboard. Fira Code pairs with Fira Sans by
// design, so the numbers read as part of the same system rather than pasted in.
const firaCode = Fira_Code({
  variable: "--font-fira-code",
  weight: ["400", "500", "600"],
  subsets: ["latin"],
});

export const metadata: Metadata = {
  icons: { icon: "/spotlight-logo.svg", apple: "/spotlight-logo.svg" },
  title: "SPOTlight — SPOT Operator Console",
  description:
    "Turns SPOT inspection missions into a ranked list of what an operator should check next.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${firaSans.variable} ${firaCode.variable} h-full antialiased`}
      // The inline script below writes data-theme before React sees the DOM.
      suppressHydrationWarning
    >
      <head>
        {/* Runs synchronously during HTML parsing, so the saved theme is
            applied before the first paint. An effect would run after paint
            and flash the wrong theme on every load. */}
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body className="min-h-full bg-canvas text-ink">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded focus:bg-surface focus:px-3 focus:py-2 focus:text-sm focus:text-ink"
        >
          Skip to content
        </a>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
