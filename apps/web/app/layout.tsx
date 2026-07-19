import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { Providers } from "@/components/providers";
import { SiteHeader } from "@/components/layout/site-header";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: {
    default: "Axon",
    template: "%s · Axon",
  },
  description:
    "A Truth Maintenance System for engineering organizations. Axon continuously verifies company knowledge against reality and detects when it becomes false.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // Dark theme is the product default (projector-friendly, dev-tool-native).
    // The `dark` class activates shadcn's dark CSS variables globally.
    <html
      lang="en"
      className={`dark ${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="bg-background text-foreground flex min-h-full flex-col">
        <Providers>
          <SiteHeader />
          <main className="flex-1">{children}</main>
        </Providers>
      </body>
    </html>
  );
}
