import { apiBase } from "./endpoints";
import type { UserRole } from "./roles";

export interface TeamAccount {
  id: string;
  email: string;
  full_name: string;
  role: UserRole;
  is_active: boolean;
}

async function request<T>(path: string, token?: string, role?: UserRole): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${apiBase()}/api/v1/auth/users${path}`, {
      method: role === undefined ? "GET" : "PATCH",
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(role === undefined ? {} : { "Content-Type": "application/json" }),
      },
      ...(role === undefined ? {} : { body: JSON.stringify({ role }) }),
    });
  } catch (cause) {
    throw new Error("Could not reach the library. Check your connection and try again.", { cause });
  }
  if (!response.ok) {
    const value: unknown = await response.json().catch(() => null);
    const detail = value && typeof value === "object" && "detail" in value ? value.detail : null;
    throw new Error(typeof detail === "string" ? detail : `Could not ${role === undefined ? "load team accounts" : "update team access"} (${response.status}).`);
  }
  return response.json() as Promise<T>;
}

export function fetchTeamAccounts(token?: string): Promise<TeamAccount[]> {
  return request("", token);
}

export function updateTeamAccountRole(id: string, role: UserRole, token?: string): Promise<TeamAccount> {
  return request(`/${encodeURIComponent(id)}/role`, token, role);
}
