import { type FormEvent, useId } from "react";
import {
  type KeyedProvider,
  type Provider,
  type ProviderResult,
  type SettingsView,
  providers,
} from "./admin";
import { type Text, providerName } from "./labels";

const keyUrls = {
  brave: "https://brave.com/search/api/",
  tavily: "https://app.tavily.com/",
  exa: "https://dashboard.exa.ai/",
};

type Props = {
  settings: SettingsView;
  busy: boolean;
  testing: boolean;
  result?: ProviderResult;
  onProvider: (provider: Provider) => void;
  onSearxng: (config: SettingsView["search"]["searxng"]) => void;
  onKey: (provider: KeyedProvider, key: string) => void;
  onRemoveKey: (provider: KeyedProvider) => void;
  onTest: (provider: Provider) => void;
  onInvalidEngines: () => void;
  t: Text;
};

export function ProviderOptions({
  settings,
  busy,
  testing,
  result,
  onProvider,
  onSearxng,
  onKey,
  onRemoveKey,
  onTest,
  onInvalidEngines,
  t,
}: Props) {
  const { provider, searxng } = settings.search;
  return (
    <div className="panel-body">
      <label className="field">
        <span>{t("provider")}</span>
        <select
          value={provider}
          disabled={busy}
          onChange={(event) =>
            onProvider(event.currentTarget.value as Provider)
          }
        >
          {providers.map((id) => (
            <option key={id} value={id}>
              {providerName(t, id)}
            </option>
          ))}
        </select>
      </label>
      {provider === "searxng" && (
        <SearxngForm
          key={JSON.stringify(searxng)}
          config={searxng}
          onSave={onSearxng}
          invalidEngines={onInvalidEngines}
          busy={busy}
          t={t}
        />
      )}
      {provider !== "auto" && (
        <KeyForm
          key={provider}
          provider={provider}
          isSet={settings.providers[provider].key_set}
          onSave={onKey}
          onRemove={onRemoveKey}
          busy={busy}
          t={t}
        />
      )}
      <div className="actions">
        <button
          type="button"
          className="button secondary"
          disabled={busy}
          onClick={() => onTest(provider)}
        >
          {t(testing ? "testing" : "testProvider")}
        </button>
      </div>
      {result?.ok && <TestResult result={result} t={t} />}
    </div>
  );
}

function SearxngForm({
  config,
  onSave,
  invalidEngines,
  busy,
  t,
}: {
  config: SettingsView["search"]["searxng"];
  onSave: Props["onSearxng"];
  invalidEngines: Props["onInvalidEngines"];
  busy: boolean;
  t: Text;
}) {
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const engines = String(data.get("engines") ?? "")
      .split(",")
      .map((name) => name.trim())
      .filter(Boolean);
    if (
      !engines.length ||
      engines.length > 20 ||
      new Set(engines).size !== engines.length ||
      engines.some((name) => name.length > 100)
    ) {
      invalidEngines();
      return;
    }
    onSave({ url: String(data.get("endpoint") ?? "").trim(), engines });
  }
  return (
    <form className="provider-form" onSubmit={submit}>
      <label className="field">
        <span>{t("searxngUrl")}</span>
        <input
          name="endpoint"
          type="url"
          defaultValue={config.url}
          maxLength={4096}
          required
          disabled={busy}
        />
      </label>
      <label className="field">
        <span>{t("searxngEngines")}</span>
        <input
          name="engines"
          type="text"
          defaultValue={config.engines.join(", ")}
          maxLength={2040}
          required
          disabled={busy}
        />
      </label>
      <div className="actions">
        <button type="submit" className="button" disabled={busy}>
          {t("saveConfiguration")}
        </button>
      </div>
    </form>
  );
}

function KeyForm({
  provider,
  isSet,
  onSave,
  onRemove,
  busy,
  t,
}: {
  provider: KeyedProvider;
  isSet: boolean;
  onSave: Props["onKey"];
  onRemove: Props["onRemoveKey"];
  busy: boolean;
  t: Text;
}) {
  const descriptionId = useId();
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const key = String(new FormData(form).get("key") ?? "").trim();
    form.reset();
    onSave(provider, key);
  }

  return (
    <form className="provider-form" onSubmit={submit}>
      <label className="field">
        <span>{t("providerKey")}</span>
        <input
          name="key"
          type="password"
          autoComplete="off"
          maxLength={8192}
          required
          disabled={busy}
          aria-describedby={descriptionId}
        />
      </label>
      <p id={descriptionId} className="key-state">
        {t(isSet ? "keyConfigured" : "keyNotConfigured")}
        {provider === "searxng" && <> {t("keyOptional")}</>}
      </p>
      <div className="actions">
        <button type="submit" className="button" disabled={busy}>
          {t("saveKey")}
        </button>
        {isSet && (
          <button
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={() => onRemove(provider)}
          >
            {t("removeKey")}
          </button>
        )}
        {provider !== "searxng" && (
          <a href={keyUrls[provider]} target="_blank" rel="noopener noreferrer">
            {t("getKey")}
          </a>
        )}
      </div>
    </form>
  );
}

function safeLink(raw: string): string | undefined {
  try {
    const url = new URL(raw);
    if (
      (url.protocol === "http:" || url.protocol === "https:") &&
      !url.username &&
      !url.password
    ) {
      return url.href;
    }
  } catch {
    return undefined;
  }
}

function TestResult({ result, t }: { result: ProviderResult; t: Text }) {
  return (
    <>
      <p className="message success" role="status">
        {t("testSucceeded", {
          source: providerName(t, result.source),
          count: result.result_count,
          elapsed: result.elapsed_ms,
        })}
      </p>
      {result.sample.length > 0 && (
        <ul className="sample-list">
          {result.sample.slice(0, 3).map(({ title, url }, index) => {
            const link = safeLink(url);
            return (
              <li key={index}>
                {link ? (
                  <a href={link} target="_blank" rel="noopener noreferrer">
                    {title || link}
                  </a>
                ) : (
                  title
                )}
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}
