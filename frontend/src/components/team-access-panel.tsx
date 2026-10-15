"use client";

import { useCallback, useEffect, useState } from "react";
import { RefreshCw, Search, ShieldCheck } from "lucide-react";
import { useAppState } from "@/lib/app-state";
import { useOptionalAuth } from "@/lib/auth";
import { USER_ROLES, type UserRole } from "@/lib/roles";
import { fetchTeamAccounts, updateTeamAccountRole, type TeamAccount } from "@/lib/team-access";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";

const ROLE_INFO: Record<UserRole, { label: string; description: string }> = {
  admin: { label: "Administrator", description: "Manage team access and all staff tools." },
  analyst: { label: "Analyst", description: "Process film, review plays, and analyze results." },
  coach: { label: "Coach", description: "Process film and review coaching labels." },
  sportsperformance: { label: "Sports performance", description: "Process film and use health and workload tools." },
  player: { label: "Player", description: "Player access; cannot upload or process film." },
  viewer: { label: "Viewer", description: "Read-only access; cannot upload or process film." },
};

export function TeamAccessPanel() {
  const { currentRole, authToken } = useAppState();
  return currentRole === "admin" ? <AdminTeamAccess token={authToken} /> : null;
}

function AdminTeamAccess({ token }: { token?: string }) {
  const auth = useOptionalAuth();
  const [accounts, setAccounts] = useState<TeamAccount[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try { setAccounts(await fetchTeamAccounts(token)); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setLoading(false); }
  }, [token]);

  useEffect(() => { void load(); }, [load]);

  const search = query.trim().toLowerCase();
  const visible = accounts.filter((account) => `${account.email} ${account.full_name} ${ROLE_INFO[account.role].label}`.toLowerCase().includes(search));
  const currentEmail = auth?.user?.email.toLowerCase();
  async function saved(account: TeamAccount) {
    setAccounts((current) => current.map((item) => item.id === account.id ? account : item));
    if (account.email.toLowerCase() === currentEmail) await auth?.refreshSession();
  }

  return <Card>
    <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
      <div><h2 className="flex items-center gap-2 text-base font-semibold"><ShieldCheck className="size-4 text-primary" aria-hidden />Team access</h2><p className="mt-2 max-w-3xl text-sm leading-relaxed text-muted-foreground">Staff create an account on this library as a Viewer. Assign a staff role here to let them upload and process film. Only administrators can change access.</p></div>
      <Button variant="outline" size="sm" disabled={loading} onClick={() => void load()}><RefreshCw className="size-3.5" aria-hidden />Refresh accounts</Button>
    </CardHeader>
    <CardContent className="flex flex-col gap-4">
      {loading ? <p role="status" className="text-sm text-muted-foreground">Loading team accounts…</p> : null}
      {error ? <div role="alert" className="rounded-lg border border-status-danger/40 p-3 text-sm"><p>{error}</p><Button variant="outline" size="sm" className="mt-3" onClick={() => void load()}>Try again</Button></div> : null}
      {accounts.length > 0 ? <><div className="relative max-w-xl"><Label htmlFor="team-account-search" className="sr-only">Find a team account</Label><Search className="pointer-events-none absolute left-3 top-3 size-3.5 text-muted-foreground" aria-hidden /><Input id="team-account-search" value={query} onChange={(event) => setQuery(event.target.value)} className="pl-9" placeholder="Find a teammate by name, email, or role…" /></div><div className="divide-y divide-border-soft rounded-lg border border-border-soft">{visible.map((account) => <TeamAccountRow key={account.id} account={account} token={token} isYou={account.email.toLowerCase() === currentEmail} onSaved={saved} />)}</div>{visible.length === 0 ? <p className="text-sm text-muted-foreground">No accounts match your search.</p> : null}<p className="text-xs text-muted-foreground">Existing sessions may show the previous role until the staff member signs out and back in. Your own session refreshes after an access change.</p></> : !loading && !error ? <p className="text-sm text-muted-foreground">No registered accounts were returned by this library.</p> : null}
    </CardContent>
  </Card>;
}

function TeamAccountRow({ account, token, isYou, onSaved }: { account: TeamAccount; token?: string; isYou: boolean; onSaved: (account: TeamAccount) => Promise<void> }) {
  const [role, setRole] = useState(account.role);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => { setRole(account.role); }, [account.role]);
  const dirty = role !== account.role;
  async function save() {
    if (pending || !dirty) return;
    setPending(true); setError(null); setNotice(null);
    try {
      const updated = await updateTeamAccountRole(account.id, role, token);
      setRole(updated.role);
      await onSaved(updated);
      setNotice("Access saved.");
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setPending(false); }
  }

  return <div role="group" className="grid gap-3 p-4 sm:grid-cols-[minmax(0,1fr)_200px_auto] sm:items-start" aria-label={`Account ${account.email}`}>
    <div className="min-w-0"><p className="break-words text-sm font-medium">{account.full_name || account.email}{isYou ? <span className="ml-2 text-xs font-normal text-muted-foreground">You</span> : null}</p><p className="mt-1 break-all text-xs text-muted-foreground">{account.email}</p>{!account.is_active ? <p className="mt-2 text-xs text-status-warn">Inactive account. Changing its role does not reactivate it.</p> : null}</div>
    <div className="flex flex-col gap-2"><Label htmlFor={`team-role-${account.id}`} className="sr-only">Role for {account.email}</Label><NativeSelect id={`team-role-${account.id}`} aria-describedby={`team-role-description-${account.id}`} className="w-full" value={role} disabled={pending} onChange={(event) => { setRole(event.target.value as UserRole); setError(null); setNotice(null); }}>{USER_ROLES.map((value) => <option key={value} value={value}>{ROLE_INFO[value].label}</option>)}</NativeSelect><p id={`team-role-description-${account.id}`} className="text-xs leading-relaxed text-muted-foreground">{ROLE_INFO[role].description}</p></div>
    <Button size="sm" variant={dirty ? "default" : "outline"} className="justify-self-start" aria-label={`Save access for ${account.email}`} disabled={pending || !dirty} onClick={() => void save()}>{pending ? "Saving…" : "Save access"}</Button>
    {error ? <p role="alert" className="text-sm text-status-danger sm:col-span-3">{error}</p> : null}{notice ? <p role="status" className="text-xs text-status-ok sm:col-span-3">{notice}</p> : null}
  </div>;
}
