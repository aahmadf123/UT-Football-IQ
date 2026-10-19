import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "./providers";
import { Toaster } from "@/components/ui/sonner";

export const metadata: Metadata = {
  title: "Football-IQ | Toledo Football Analytics",
  description:
    "Toledo Football computer vision platform — practice film intelligence, player tracking, and coaching analytics.",
  icons: {
    icon: "/brand/Secondary_Logo_for_Light_Background.png",
    shortcut: "/brand/Secondary_Logo_for_Light_Background.png",
    apple: "/brand/Secondary_Logo_for_Light_Background.png",
  },
  // Read by the deploy smoke check and compared with the API's
  // /api/v1/system/version, so a half-finished deploy is caught.
  other: { "build-sha": process.env.NEXT_PUBLIC_GIT_SHA ?? "dev" },
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
        <Toaster />
      </body>
    </html>
  );
}
