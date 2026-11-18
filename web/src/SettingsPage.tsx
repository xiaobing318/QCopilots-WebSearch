import { useEffect, useId, useState } from "react";
import {
  admin,
  ApiError,
  type KeyedProvider,
  type Listener,
  type Provider,
  type ProviderResult,
  type ServiceStatus,
  type SettingsPatch,
  type SettingsView,
} from "./admin";
import { errorLabel, providerName, type Text } from "./labels";
import { ProviderOptions } from "./ProviderOptions";

type Notice = { key: string; variant: "error" | "success" };
type Props = { csrf: string; t: Text; onExpired: () => void };

export function SettingsPage({ csrf, t, onExpired }: Props) {
  const [settings, setSettings] = useState<SettingsView>();
  const [status, setStatus] = useState<ServiceStatus>();
  const [statusError, setStatusError] = useState<string>();
  const [notice, setNotice] = useState<Notice>();
  const [busy, setBusy] = useState<"change" | "test" | "status">();
  const [result, setResult] = useState<ProviderResult>();

  useEffect(() => {
    const abort = new AbortController();
    void admin.settings(abort.signal).then(
      (settings) => {
        if (!abort.signal.aborted) setSettings(settings);
      },
      (failure: unknown) => {
        if (abort.signal.aborted) return;
        if (failure instanceof ApiError && failure.code === "unauthorized")
          onExpired();
        else setNotice({ key: errorLabel(failure), variant: "error" });
      },
    );
    void admin.status(abort.signal).then(
      (status) => {
        if (!abort.signal.aborted) setStatus(status);
      },
      (failure: unknown) => {
        if (abort.signal.aborted) return;
        if (failure instanceof ApiError && failure.code === "unauthorized")
          onExpired();
        else setStatusError(errorLabel(failure));
      },
    );
    return () => abort.abort();
  }, [onExpired]);

  async function perform(
    action: NonNullable<typeof busy>,
    task: () => Promise<void>,
  ) {
    if (busy) return;
    setNotice(undefined);
    setBusy(action);
    try {
      await task();
    } catch (failure) {
      if (failure instanceof ApiError && failure.code === "unauthorized") {
        onExpired();
      } else {
        setNotice({ key: errorLabel(failure), variant: "error" });
      }
    } finally {
      setBusy(undefined);
    }
  }

  function update(patch: SettingsPatch) {
    if (!settings) return;
    void perform("change", async () => {
      try {
        setSettings(await admin.update(settings.revision, patch, csrf));
        if (patch.search) setResult(undefined);
        setNotice({ key: "saved", variant: "success" });
      } catch (failure) {
        if (
          failure instanceof ApiError &&
          failure.code === "revision_conflict"
        ) {
          setSettings(await admin.settings());
          setResult(undefined);
        }
        throw failure;
      }
    });
  }

  function saveKey(provider: KeyedProvider, key: string) {
    void perform("change", async () => {
      setSettings(await admin.saveKey(provider, key, csrf));
      setResult(undefined);
      setNotice({ key: "keySaved", variant: "success" });
    });
  }

  function removeKey(provider: KeyedProvider) {
    void perform("change", async () => {
      setSettings(await admin.removeKey(provider, csrf));
      setResult(undefined);
      setNotice({ key: "keyRemoved", variant: "success" });
    });
  }

  async function refreshStatus() {
    try {
      setStatus(await admin.status());
      setStatusError(undefined);
    } catch (failure) {
      if (failure instanceof ApiError && failure.code === "unauthorized")
        throw failure;
      setStatusError(errorLabel(failure));
    }
  }

  function testProvider(provider: Provider) {
    void perform("test", async () => {
      setResult(undefined);
      const response = await admin.test(provider, csrf);
      if (response.ok) setResult(response);
      else
        setNotice({
          key: "errors." + (response.error?.code ?? "unknown"),
          variant: "error",
        });
      await refreshStatus();
    });
  }

  return (
    <>
      <div className="intro">
        <h1>{t("settingsTitle")}</h1>
        <p>{t("settingsDescription")}</p>
      </div>
      {notice && (
        <p
          className={"message " + notice.variant}
          role={notice.variant === "error" ? "alert" : "status"}
        >
          {t(notice.key)}
        </p>
      )}
      {settings ? (
        <>
          <section className="panel" aria-labelledby="network-heading">
            <div className="panel-heading">
              <h2 id="network-heading">{t("networkAccess")}</h2>
            </div>
            <Toggle
              label={t("openToggle")}
              description={t("openDescription")}
              active={settings.tools.open.enabled}
              disabled={Boolean(busy)}
              onChange={(enabled) => update({ tools: { open: { enabled } } })}
            />
            <Toggle
              label={t("searchToggle")}
              description={t("searchDescription")}
              active={settings.tools.search.enabled}
              disabled={Boolean(busy)}
              onChange={(enabled) => update({ tools: { search: { enabled } } })}
            />
          </section>
          <section className="panel" aria-labelledby="search-heading">
            <div className="panel-heading">
              <h2 id="search-heading">{t("webSearch")}</h2>
            </div>
            <ProviderOptions
              settings={settings}
              busy={Boolean(busy)}
              testing={busy === "test"}
              result={result}
              onProvider={(provider) => update({ search: { provider } })}
              onSearxng={(searxng) => update({ search: { searxng } })}
              onKey={saveKey}
              onRemoveKey={removeKey}
              onTest={testProvider}
              onInvalidEngines={() =>
                setNotice({ key: "invalidEngines", variant: "error" })
              }
              t={t}
            />
            <Toggle
              label={t("askToggle")}
              description={t("askDescription")}
              active={settings.search_without_asking}
              disabled={Boolean(busy)}
              onChange={(value) => update({ search_without_asking: value })}
            />
          </section>
          <StatusPanel
            status={status}
            error={statusError}
            busy={Boolean(busy)}
            refresh={() => void perform("status", refreshStatus)}
            t={t}
          />
        </>
      ) : (
        <div className="loading">
          {notice ? (
            <button
              type="button"
              className="button secondary"
              disabled={Boolean(busy)}
              onClick={() =>
                void perform("change", async () =>
                  setSettings(await admin.settings()),
                )
              }
            >
              {t("retry")}
            </button>
          ) : (
            <p role="status">{t("loading")}</p>
          )}
        </div>
      )}
    </>
  );
}

function Toggle({
  label,
  description,
  active,
  disabled,
  onChange,
}: {
  label: string;
  description: string;
  active: boolean;
  disabled: boolean;
  onChange: (checked: boolean) => void;
}) {
  const id = useId();
  return (
    <label className="setting-row">
      <span className="row-copy">
        <strong id={id}>{label}</strong>
        <span id={id + "-description"}>{description}</span>
      </span>
      <input
        type="checkbox"
        role="switch"
        className="switch"
        checked={active}
        disabled={disabled}
        aria-labelledby={id}
        aria-describedby={id + "-description"}
        onChange={(event) => onChange(event.currentTarget.checked)}
      />
    </label>
  );
}

const listenerKeys: Record<Listener, string> = {
  business: "listenerBusiness",
  control: "listenerControl",
  admin: "listenerAdmin",
};

function StatusPanel({
  status,
  error,
  busy,
  refresh,
  t,
}: {
  status?: ServiceStatus;
  error?: string;
  busy: boolean;
  refresh: () => void;
  t: Text;
}) {
  return (
    <section className="panel" aria-labelledby="status-heading">
      <div className="panel-heading">
        <h2 id="status-heading">{t("statusTitle")}</h2>
      </div>
      <div className="panel-body">
        {error && (
          <p className="message error" role="alert">
            {t(error)}
          </p>
        )}
        {status?.settings_error && (
          <p className="message error" role="alert">
            {t("settingsError")}
          </p>
        )}
        {status ? (
          <dl className="status-list">
            <div>
              <dt>{t("statusVersion")}</dt>
              <dd>{status.version}</dd>
            </div>
            {(Object.keys(listenerKeys) as Listener[]).map((name) => {
              const { host, port } = status.listeners[name];
              return (
                <div key={name}>
                  <dt>{t(listenerKeys[name])}</dt>
                  <dd>
                    {host.includes(":") ? "[" + host + "]" : host}:{port}
                  </dd>
                </div>
              );
            })}
            {Object.keys(status.cooldown).length > 0 && (
              <div>
                <dt>{t("statusCooldown")}</dt>
                <dd>
                  <ul className="cooldown-list">
                    {Object.entries(status.cooldown).map(([name, details]) => (
                      <li key={name}>
                        {providerName(t, name)} ·{" "}
                        {t("cooldownSeconds", {
                          seconds: details.retry_after_seconds,
                        })}
                      </li>
                    ))}
                  </ul>
                </dd>
              </div>
            )}
          </dl>
        ) : (
          !error && <p role="status">{t("loading")}</p>
        )}
        <div className="actions">
          <button
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={refresh}
          >
            {t("refreshStatus")}
          </button>
        </div>
      </div>
    </section>
  );
}
