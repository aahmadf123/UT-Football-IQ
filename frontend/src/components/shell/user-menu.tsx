"use client";

import Link from "next/link";
import { LogIn, LogOut } from "lucide-react";
import { useOptionalAuth } from "@/lib/auth";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/**
 * Avatar dropdown: who is signed in, with which role, and sign out — replaces
 * the old inert initials pill + separate sign-out icon.
 */
export function UserMenu() {
  const auth = useOptionalAuth();
  const user = auth?.user ?? null;
  const token = auth?.token;

  const initials = (
    (user?.email?.[0] ?? "U") + (user?.email?.split("@")[0]?.[1] ?? "T")
  ).toUpperCase();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="icon"
          className="size-10 rounded-full border-border-soft bg-secondary/60 font-display text-sm font-semibold tracking-wide"
          aria-label="Account menu"
        >
          {initials}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-52">
        {user ? (
          <>
            <DropdownMenuLabel className="flex flex-col gap-0.5">
              <span className="truncate text-sm font-medium">{user.email}</span>
              <span className="text-xs font-normal capitalize text-muted-foreground">
                {user.role}
              </span>
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
          </>
        ) : (
          <>
            <DropdownMenuLabel className="text-sm font-normal text-muted-foreground">
              Not signed in
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
          </>
        )}
        {token ? (
          <DropdownMenuItem
            onSelect={() => {
              auth?.logout();
              window.location.replace("/login/");
            }}
          >
            <LogOut />
            Sign out
          </DropdownMenuItem>
        ) : (
          <DropdownMenuItem asChild>
            <Link href="/login">
              <LogIn />
              Sign in
            </Link>
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
