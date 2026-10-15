"use client";

import Link from "next/link";
import Image from "next/image";
import { Menu, Search } from "lucide-react";
import type { PageKey } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { AlertsBell } from "@/components/shell/alerts-bell";
import { UserMenu } from "@/components/shell/user-menu";
import { useWorkspaceContext } from "@/lib/workspace-context";

/**
 * Slim system chrome: brand, alerts inbox, account. Page identity lives in
 * PageHeader; film filters live on the pages that use them.
 */
export function AppTopbar({
  activePage,
  onMenuClick,
}: {
  activePage: PageKey;
  onMenuClick: () => void;
}) {
  const workspace = useWorkspaceContext();
  return (
    <header className="sticky top-0 z-20 flex h-16 items-center gap-3 border-b border-border-soft bg-[oklch(0.145_0.028_252/0.95)] px-3 backdrop-blur-md lg:px-5">
      <Button
        variant="ghost"
        size="icon"
        className="size-10 lg:hidden"
        onClick={onMenuClick}
        aria-label="Open navigation"
      >
        <Menu className="size-5" />
      </Button>

      <Link href="/" className="flex items-center" aria-label="Football IQ home">
        <Image
          src="/brand/Secondary_Logo_for_Dark_Background.png"
          width={760}
          height={252}
          alt="Football-IQ"
          className="h-7 w-auto object-contain"
          priority
        />
      </Link>

      <span className="hidden border-l border-border-soft pl-4 md:block"><span className="section-eyebrow block">Football operations</span><span className="mt-0.5 block text-xs font-medium text-foreground">Toledo Football</span></span>

      <div className="flex-1" />

      <Link href="/film-room/?tab=browse#library-search" className="mr-2 hidden min-h-9 items-center gap-3 rounded-md border border-border-soft bg-background/50 px-3 text-xs text-muted-foreground hover:border-primary/30 hover:text-foreground lg:flex"><Search className="size-3.5" aria-hidden /> Search film library</Link>
      <Link href="/settings/" className="mr-2 hidden text-xs text-muted-foreground hover:text-foreground sm:block">{workspace.settingsLabel}</Link>

      <AlertsBell active={activePage === "alerts"} />
      <UserMenu />
    </header>
  );
}
