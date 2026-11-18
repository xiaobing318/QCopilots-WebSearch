import en from "../locales/en.json";
import zhCN from "../locales/zh-CN.json";

export type Language = "en" | "zh-CN";

const storageKey = "qcws.language";
const catalogs: Record<Language, Record<string, string>> = {
  en,
  "zh-CN": zhCN,
};

export function getLanguage(): Language {
  try {
    const saved = window.localStorage.getItem(storageKey);
    if (saved === "en" || saved === "zh-CN") return saved;
  } catch {
    // A browser can disable storage while keeping the current page usable.
  }

  for (const preferred of navigator.languages) {
    const language = preferred.toLowerCase().split("-")[0];
    if (language === "zh") return "zh-CN";
    if (language === "en") return "en";
  }
  return "en";
}

export function applyLanguage(language: Language): void {
  document.documentElement.lang = language;
  try {
    window.localStorage.setItem(storageKey, language);
  } catch {
    // The document still reflects the chosen language when storage is unavailable.
  }
}

export function translate(
  language: Language,
  key: string,
  values: Record<string, string | number> = {},
): string {
  const template =
    [catalogs[language], catalogs.en].find((catalog) =>
      Object.hasOwn(catalog, key),
    )?.[key] ?? key;
  return template.replace(/\{(\w+)\}/g, (placeholder: string, name: string) =>
    Object.hasOwn(values, name) ? String(values[name]) : placeholder,
  );
}
