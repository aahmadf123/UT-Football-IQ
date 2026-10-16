/**
 * Errors coaches can read.
 *
 * Every failed API call becomes an {@link ApiError} whose `message` is plain
 * language ("Couldn't save the play. Someone else changed it — reload and try
 * again."), so any existing `err.message` display is already safe to show.
 * The raw URL, status and response body live on `technical` and go to the
 * browser console only — never into the UI.
 */

export type ApiErrorKind =
  | "offline"
  | "auth"
  | "forbidden"
  | "not_found"
  | "conflict"
  | "too_large"
  | "invalid"
  | "busy"
  | "server";

const REASONS: Record<ApiErrorKind, string> = {
  offline: "The server can't be reached right now. Check your connection and try again.",
  auth: "Your sign-in has expired. Please sign in again.",
  forbidden: "Your account doesn't have access to this. Ask an admin if you need it.",
  not_found: "It may have been removed. Refresh the page to see the latest.",
  conflict: "Someone else changed this at the same time. Refresh and try again.",
  too_large: "The file is too large to upload.",
  invalid: "Some of the information wasn't accepted. Check it and try again.",
  busy: "The system is busy processing film. Try again in a minute.",
  server: "Something went wrong on our side. Try again in a moment.",
};

export function kindForStatus(status: number): ApiErrorKind {
  if (status === 0) return "offline";
  if (status === 401) return "auth";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 409 || status === 412) return "conflict";
  if (status === 413) return "too_large";
  if (status === 400 || status === 422) return "invalid";
  if (status === 429 || status === 503) return "busy";
  return "server";
}

/**
 * A short, human sentence the server wrote for this failure, if there is one.
 * FastAPI puts it in `detail` (a string, or an object with `message`);
 * validation errors (lists) and anything that looks like code are ignored.
 */
export function serverSentence(body: string): string | null {
  let detail: unknown = body;
  try {
    detail = (JSON.parse(body) as { detail?: unknown }).detail ?? null;
  } catch {
    // Not JSON: plain-text bodies are usually proxies' HTML/error pages.
    return null;
  }
  if (detail && typeof detail === "object" && "message" in detail) {
    detail = (detail as { message?: unknown }).message;
  }
  if (typeof detail !== "string") return null;
  const text = detail.trim();
  const looksTechnical = /[{}<>]|Traceback|Exception|\bSQL\b|_[a-z]+_|^[a-z_]+$/.test(text);
  if (!text || text.length > 160 || looksTechnical) return null;
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

export class ApiError extends Error {
  readonly status: number;
  readonly kind: ApiErrorKind;
  /** Raw status/URL/body for debugging. Logged, never rendered. */
  readonly technical: string;

  constructor(options: { status: number; action: string; body?: string; technical?: string }) {
    const kind = kindForStatus(options.status);
    // Prefer the server's own sentence when it wrote one for people ("This
    // recording is already in the library."), else our generic reason.
    const specific =
      kind === "auth" || kind === "offline" || kind === "server"
        ? null
        : serverSentence(options.body ?? "");
    super(`Couldn't ${options.action}. ${specific ?? REASONS[kind]}`);
    this.name = "ApiError";
    this.status = options.status;
    this.kind = kind;
    this.technical = options.technical ?? `HTTP ${options.status}: ${options.body ?? ""}`.slice(0, 2000);
    if (typeof console !== "undefined") console.debug("[api]", options.action, this.technical);
  }
}

/** Build an {@link ApiError} from a failed `fetch` response. */
export async function apiErrorFromResponse(response: Response, action: string): Promise<ApiError> {
  let body = "";
  try {
    body =
      typeof response.text === "function"
        ? await response.text()
        : JSON.stringify(await (response as { json: () => Promise<unknown> }).json());
  } catch {
    // Body already consumed or stream failed: the status alone is enough.
  }
  return new ApiError({
    status: response.status,
    action,
    body,
    technical: `${response.status} ${response.url}: ${body}`.slice(0, 2000),
  });
}

/** A network-level failure (no response at all). */
export function offlineError(action: string, cause?: unknown): ApiError {
  return new ApiError({ status: 0, action, technical: String(cause ?? "network error") });
}

/**
 * Plain-language text for any thrown value. Use this wherever an error is
 * shown to a person; it also cleans up errors not produced by this module
 * (e.g. the browser's "Failed to fetch").
 */
export function friendlyMessage(error: unknown, action = "finish that"): string {
  if (error instanceof ApiError) return error.message;
  const message = error instanceof Error ? error.message : typeof error === "string" ? error : "";
  if (/failed to fetch|networkerror|load failed|network request failed/i.test(message)) {
    return `Couldn't ${action}. ${REASONS.offline}`;
  }
  // Legacy "Thing failed (503): {json}" strings from code not yet migrated.
  const legacy = /\((\d{3})\)/.exec(message);
  if (legacy || /[{}]|https?:\/\//.test(message) || !message) {
    return `Couldn't ${action}. ${REASONS[kindForStatus(legacy ? Number(legacy[1]) : 500)]}`;
  }
  return message;
}
