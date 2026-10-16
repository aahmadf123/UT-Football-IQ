import { apiBase } from "./endpoints";
import { apiErrorFromResponse } from "./api-error";
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
    throw await apiErrorFromResponse(response, role === undefined ? "load team accounts" : "update team access");
  }
  return response.json() as Promise<T>;
}

export function fetchTeamAccounts(token?: string): Promise<TeamAccount[]> {
  return request("", token);
}

export function updateTeamAccountRole(id: string, role: UserRole, token?: string): Promise<TeamAccount> {
  return request(`/${encodeURIComponent(id)}/role`, token, role);
}
