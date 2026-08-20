import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Toaster } from "@/components/ui/sonner";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

// Monospace matters more here than in most applications: SQL, trace ids, chunk
// ids, and row counts are all read character by character, and a proportional
// font makes columns of them impossible to scan.
const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "EAIP Console",
  description: "Control plane for the Enterprise AI Integration Platform.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // `dark` is applied unconditionally rather than following the system
    // preference. A control plane is somewhere people sit for long stretches
    // reading dense output, and the palette was designed for that surface —
    // the light tokens exist so a theme toggle is a small change later, not a
    // repaint.
    <html
      lang="en"
      className={`dark ${geistSans.variable} ${geistMono.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <body className="bg-background text-foreground min-h-full flex flex-col">
        {children}
        <Toaster />
      </body>
    </html>
  );
}
