import type { Metadata } from "next";
import { Inter, Geist_Mono } from "next/font/google";
import "./globals.css";
import { themeInitScript, ThemeToggle } from "@/components/ThemeToggle";
import { PathProvider } from "@/components/PathProvider";
import { StoreProvider } from "@/components/StoreProvider";
import { AuthGate } from "@/components/AuthGate";

// Match the NN Operations dashboard, which uses Inter. Keep the CSS-var name
// (--font-geist-sans) so globals.css + every component reference stays valid.
const geistSans = Inter({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Operator — NN Operations",
  description: "Research, list, and optimize — the NN Operations product pipeline.",
};

// Root layout is the Operation-System chassis only — it owns <html>/<body>, theme
// init, and the shared store-path context. It deliberately renders NO app chrome:
// each app (Research & Listing, Product Feed) supplies its own sidebar/header via a
// route-group layout, so they are genuinely standalone surfaces that merely share
// this one root (and, later, data between them).
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} h-full`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body className="h-full antialiased">
        <PathProvider>
          <StoreProvider>
            <AuthGate>{children}</AuthGate>
          </StoreProvider>
        </PathProvider>
        {/* The empty per-app header bands were removed; the theme toggle now floats globally
            (bottom-right) so dark mode stays reachable on every page without a wasted top row. */}
        <div className="fixed bottom-4 right-4 z-40">
          <ThemeToggle />
        </div>
      </body>
    </html>
  );
}
