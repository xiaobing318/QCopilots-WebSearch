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
export type ClientCredentialSource = "manual" | "file" | "environment";
export type ClientRegistryView = {
  revision: string;
  clients: {
    id: string;
    source: ClientCredentialSource;
    can_reveal?: boolean;
  }[];
  pending_restart: boolean;
};
export type ClientReveal = {
  id: string;
  token: string;
  revision: string;
  active: boolean;
};
export type ApiErrorDetails = {
  reason?: string;
  parameters?: { client_id?: string };
  status?: number;
  retry_after_seconds?: number;
};
export type ApiErrorData = Omit<ApiErrorDetails, "status"> & { code: string };
export type ProviderResult = {
  ok: boolean;
  source: string;
  result_count: number;
  elapsed_ms: number;
  sample: { title: string; url: string }[];
  error?: ApiErrorData;
};
export type Listener = "business" | "control" | "admin";
export type ServiceStatus = {
  version: string;
  mcp_url?: string;
  listeners: Record<Listener, { host: string; port: number }>;
  cooldown: Partial<
    Record<"duckduckgo" | "bing", { retry_after_seconds: number }>
  >;
  settings_error: string | null;
  settings_error_detail?: ApiErrorData | null;
};

export class ApiError extends Error {
  readonly code: string;
  readonly reason?: string;
  readonly parameters: Readonly<{ client_id?: string }>;
  readonly status?: number;
  readonly retry_after_seconds?: number;

  constructor(code: string, details: ApiErrorDetails = {}) {
    const safeCode = identifier(code) ?? "unknown";
    super(safeCode);
    this.name = "ApiError";
    this.code = safeCode;
    this.reason = identifier(details.reason);
    const clientId = details.parameters?.client_id;
    this.parameters = Object.freeze(
      typeof clientId === "string" && /^[A-Za-z0-9_.-]{1,64}$/.test(clientId)
        ? { client_id: clientId }
        : {},
    );
    this.status =
      Number.isInteger(details.status) &&
      details.status! >= 100 &&
      details.status! <= 599
        ? details.status
        : undefined;
    this.retry_after_seconds = retrySeconds(details.retry_after_seconds);
  }

  static fromDetails(
    value: unknown,
    status?: number,
    retryAfter?: number,
  ): ApiError {
    const details = record(value);
    return new ApiError(identifier(details?.code) ?? statusCode(status), {
      reason: identifier(details?.reason),
      parameters: record(details?.parameters) ?? undefined,
      status,
      retry_after_seconds:
        retrySeconds(details?.retry_after_seconds) ?? retryAfter,
    });
  }
}

function identifier(value: unknown): string | undefined {
  return typeof value === "string" && /^[a-z0-9_]{1,64}$/.test(value)
    ? value
    : undefined;
}

function record(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function retrySeconds(value: unknown): number | undefined {
  return typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= 0 &&
    value <= 2147483647
    ? value
    : undefined;
}

function statusCode(status?: number): string {
  switch (status) {
    case 400:
      return "invalid_argument";
    case 401:
      return "unauthorized";
    case 403:
      return "forbidden";
    case 404:
      return "not_found";
    case 413:
      return "payload_too_large";
    case 429:
      return "rate_limited";
    case 500:
      return "internal_error";
    default:
      return "unknown";
  }
}

function cancelled(failure: unknown, signal?: AbortSignal): boolean {
  return Boolean(signal?.aborted) || record(failure)?.name === "AbortError";
}

async function request<T>(
  route: string,
  method = "GET",
  body?: object,
  csrf?: string,
  signal?: AbortSignal,
  validate?: (data: unknown) => boolean,
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["Content-Type"] = "application/json";
  if (csrf) headers["X-QCWS-CSRF"] = csrf;
  let response: Response;
  try {
    response = await fetch("/admin/api/v1" + route, {
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (failure) {
    throw new ApiError(
      cancelled(failure, signal) ? "request_cancelled" : "network_error",
    );
  }
  let data: unknown;
  let unreadable = false;
  try {
    data = await response.json();
  } catch (failure) {
    if (cancelled(failure, signal))
      throw new ApiError("request_cancelled", { status: response.status });
    if (response.ok)
      throw new ApiError("invalid_response", { status: response.status });
    unreadable = true;
  }
  if (signal?.aborted)
    throw new ApiError("request_cancelled", { status: response.status });
  if (!response.ok) {
    const retryHeader = response.headers.get("Retry-After");
    const retryAfter =
      retryHeader && /^\d+$/.test(retryHeader)
        ? retrySeconds(Number(retryHeader))
        : undefined;
    if (unreadable && ![401, 403, 404, 409, 413, 429].includes(response.status))
      throw new ApiError("invalid_response", {
        status: response.status,
        retry_after_seconds: retryAfter,
      });
    throw ApiError.fromDetails(
      record(data)?.error,
      response.status,
      retryAfter,
    );
  }
  if (!record(data) || (validate && !validate(data)))
    throw new ApiError("invalid_response", { status: response.status });
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
  clients: (signal?: AbortSignal) =>
    request<ClientRegistryView>(
      "/clients",
      "GET",
      undefined,
      undefined,
      signal,
    ),
  addClient: (revision: string, id: string, token: string, csrf: string) =>
    request<ClientRegistryView>(
      "/clients",
      "POST",
      { revision, id, token },
      csrf,
    ),
  replaceClient: (revision: string, id: string, token: string, csrf: string) =>
    request<ClientRegistryView>(
      "/clients",
      "PUT",
      { revision, id, token },
      csrf,
    ),
  deleteClient: (revision: string, id: string, csrf: string) =>
    request<ClientRegistryView>("/clients", "DELETE", { revision, id }, csrf),
  revealClient: async (
    revision: string,
    id: string,
    csrf: string,
    signal?: AbortSignal,
  ): Promise<ClientReveal> => {
    const data = await request<ClientReveal>(
      "/clients/reveal",
      "POST",
      { revision, id },
      csrf,
      signal,
      (candidate) => {
        const value = record(candidate);
        return Boolean(
          value &&
          value.id === id &&
          value.revision === revision &&
          typeof value.id === "string" &&
          /^[A-Za-z0-9_.-]{1,64}$/.test(value.id) &&
          typeof value.token === "string" &&
          /^[\x21-\x7e]{1,8192}$/.test(value.token) &&
          typeof value.active === "boolean",
        );
      },
    );
    return { id, revision, token: data.token, active: data.active };
  },
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
