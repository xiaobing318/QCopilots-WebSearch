import { ApiError } from "./admin";
import { type Language, translate } from "./i18n/i18n";

export type Text = (
  key: string,
  values?: Record<string, string | number>,
) => string;

export function localized(language: Language): Text {
  return (key, values) => {
    const value = translate(language, key, values);
    return value === key && key.startsWith("errors.")
      ? translate(language, "errors.unknown")
      : value;
  };
}

export function errorLabel(error: unknown): string {
  return "errors." + (error instanceof ApiError ? error.code : "unknown");
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
