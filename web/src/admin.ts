export const providers = [
  "auto",
  "searxng",
  "brave",
  "tavily",
  "exa",
  "perplexity",
  "deepseek",
] as const;
export type Provider = (typeof providers)[number];
export type KeyedProvider = Exclude<Provider, "auto">;
type Tool = { enabled: boolean };

export type SettingsView = {
  schema_version: 1;
  tools: { search: Tool; open: Tool };
  search_without_asking: boolean;
  search: {
    provider: Provider;
    searxng: { url: string; engines: string[] };
  };
  revision: string;
  providers: Record<KeyedProvider, { key_set: boolean }>;
};

export type SettingsPatch = {
  tools?: Partial<SettingsView["tools"]>;
  search_without_asking?: boolean;
  search?: Partial<SettingsView["search"]>;
};
export type Session = { csrf: string };
export type ProviderResult = {
  ok: boolean;
  source: string;
  result_count: number;
  elapsed_ms: number;
  sample: { title: string; url: string }[];
  error?: { code: string };
};
export type Listener = "business" | "control" | "admin";
export type ServiceStatus = {
  version: string;
  listeners: Record<Listener, { host: string; port: number }>;
  cooldown: Partial<
    Record<"duckduckgo" | "bing", { retry_after_seconds: number }>
  >;
  settings_error: string | null;
};

export class ApiError extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}

function errorCode(data: unknown, status: number): string {
  if (typeof data === "object" && data !== null && "error" in data) {
    const error = data.error;
    if (
      typeof error === "object" &&
      error !== null &&
      "code" in error &&
      typeof error.code === "string"
    ) {
      return error.code;
    }
  }
  switch (status) {
    case 401:
      return "unauthorized";
    case 403:
      return "forbidden";
    case 404:
      return "not_found";
    case 409:
      return "revision_conflict";
    case 413:
      return "payload_too_large";
    case 429:
      return "rate_limited";
    default:
      return "unknown";
  }
}

async function request<T>(
  route: string,
  method = "GET",
  body?: object,
  csrf?: string,
  signal?: AbortSignal,
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["Content-Type"] = "application/json";
  if (csrf) headers["X-QCWS-CSRF"] = csrf;
  const response = await fetch("/admin/api/v1" + route, {
    method,
    headers,
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
    signal,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(errorCode(data, response.status));
  if (data === null) throw new ApiError("unknown");
  return data as T;
}

export function readLoginCode(): string | null {
  const fragment = window.location.hash;
  if (!fragment.startsWith("#login=")) return null;
  // Clear the one-time credential before issuing a network request or rendering the page.
  window.history.replaceState(
    window.history.state,
    "",
    window.location.pathname + window.location.search,
  );
  return fragment.slice("#login=".length);
}

export const admin = {
  session: (signal?: AbortSignal) =>
    request<Session>("/session", "GET", undefined, undefined, signal),
  signIn: (
    credential: { code: string } | { token: string },
    signal?: AbortSignal,
  ) => request<Session>("/session", "POST", credential, undefined, signal),
  signOut: (csrf: string) =>
    request<{ ok: true }>("/session", "DELETE", undefined, csrf),
  settings: (signal?: AbortSignal) =>
    request<SettingsView>("/settings", "GET", undefined, undefined, signal),
  update: (revision: string, patch: SettingsPatch, csrf: string) =>
    request<SettingsView>("/settings", "PUT", { revision, patch }, csrf),
  saveKey: (id: KeyedProvider, key: string, csrf: string) =>
    request<SettingsView>("/providers/" + id + "/key", "PUT", { key }, csrf),
  removeKey: (id: KeyedProvider, csrf: string) =>
    request<SettingsView>(
      "/providers/" + id + "/key",
      "DELETE",
      undefined,
      csrf,
    ),
  test: (id: Provider, csrf: string) =>
    request<ProviderResult>("/providers/" + id + "/test", "POST", {}, csrf),
  status: (signal?: AbortSignal) =>
    request<ServiceStatus>("/status", "GET", undefined, undefined, signal),
};
