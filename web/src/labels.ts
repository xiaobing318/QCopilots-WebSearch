import { ApiError } from "./admin";
import { type Language, translate } from "./i18n/i18n";
import en from "./locales/en.json";

export type Text = (
  key: string,
  values?: Record<string, string | number>,
) => string;
export type Message = {
  key: string;
  values?: Record<string, string | number>;
  context?: string;
  retryAfter?: number;
  code?: string;
  status?: number;
};

export function localized(language: Language): Text {
  return (key, values) => {
    const value = translate(language, key, values);
    return value === key && key.startsWith("errors.")
      ? translate(language, "errors.unknown")
      : value;
  };
}

export function errorLabel(error: unknown): string {
  return errorMessage(error).key;
}

export function errorMessage(error: unknown, context?: string): Message {
  const failure = error instanceof ApiError ? error : new ApiError("unknown");
  const codeKey = "errors." + failure.code;
  const reasonKey = failure.reason
    ? "errors.reason." + failure.reason
    : undefined;
  let key =
    reasonKey && Object.hasOwn(en, reasonKey)
      ? reasonKey
      : Object.hasOwn(en, codeKey)
        ? codeKey
        : "errors.unknown";
  if (
    key === "errors.reason.client_credential_duplicate" &&
    !failure.parameters.client_id
  )
    key = "errors.reason.client_credential_duplicate_generic";
  const contextKey = context ? "errorContext." + context : undefined;
  return {
    key,
    values: failure.parameters.client_id
      ? { client_id: failure.parameters.client_id }
      : undefined,
    context:
      contextKey && Object.hasOwn(en, contextKey) ? contextKey : undefined,
    retryAfter: failure.retry_after_seconds,
    code: failure.code,
    status: failure.status,
  };
}

export function renderMessage(t: Text, message: Message): string {
  const parts = [
    message.context ? t(message.context) : undefined,
    t(message.key, message.values),
  ];
  if (message.key === "errors.unknown") {
    const reference = new ApiError(message.code ?? "unknown", {
      status: message.status,
    });
    parts.push(
      reference.status === undefined
        ? t("errorReference.code", { code: reference.code })
        : t("errorReference.withStatus", {
            code: reference.code,
            status: reference.status,
          }),
    );
  }
  if (message.retryAfter !== undefined && message.retryAfter > 0)
    parts.push(t("errors.retryAfter", { seconds: message.retryAfter }));
  return parts.filter(Boolean).join(" ");
}

const providerKeys: Record<string, string> = {
  auto: "providerAuto",
  searxng: "providerSearxng",
  brave: "providerBrave",
  tavily: "providerTavily",
  exa: "providerExa",
  perplexity: "providerPerplexity",
  deepseek: "providerDeepSeek",
};

export function providerName(t: Text, name: string): string {
  if (name === "duckduckgo") return "DuckDuckGo";
  if (name === "bing") return "Bing";
  return Object.hasOwn(providerKeys, name) ? t(providerKeys[name]) : name;
}
