import { type FormEvent, useCallback, useEffect, useState } from "react";
import { admin, ApiError, type Session } from "./admin";
import { applyLanguage, getLanguage, type Language } from "./i18n/i18n";
import { errorLabel, localized, type Text } from "./labels";
import { SettingsPage } from "./SettingsPage";

type State =
  | { mode: "loading" }
  | { mode: "sign-in"; error?: string }
  | { mode: "settings"; session: Session };

export function App({ loginCode = null }: { loginCode?: string | null }) {
  const [language, setLanguage] = useState(getLanguage);
  const [state, setState] = useState<State>({ mode: "loading" });
  const [pending, setPending] = useState(false);
  const [mutationPending, setMutationPending] = useState(false);
  const [error, setError] = useState<string>();
  const t = localized(language);

  useEffect(() => {
    applyLanguage(language);
    document.title = localized(language)("brand");
  }, [language]);

  useEffect(() => {
    const abort = new AbortController();
    const session =
      loginCode === null
        ? admin.session(abort.signal)
        : admin.signIn({ code: loginCode }, abort.signal);
    void session.then(
      (session) => {
        if (!abort.signal.aborted) setState({ mode: "settings", session });
      },
      (failure: unknown) => {
        if (abort.signal.aborted) return;
        const expected =
          loginCode === null &&
          failure instanceof ApiError &&
          failure.code === "unauthorized";
        setState({
          mode: "sign-in",
          error: expected ? undefined : errorLabel(failure),
        });
      },
    );
    return () => abort.abort();
  }, [loginCode]);

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const token = String(new FormData(form).get("token") ?? "").trim();
    form.reset();
    setPending(true);
    setState({ mode: "sign-in" });
    try {
      setState({ mode: "settings", session: await admin.signIn({ token }) });
    } catch (failure) {
      setState({ mode: "sign-in", error: errorLabel(failure) });
    } finally {
      setPending(false);
    }
  }

  const sessionExpired = useCallback(() => {
    setError(undefined);
    setState({ mode: "sign-in", error: "errors.unauthorized" });
  }, []);

  async function signOut() {
    if (state.mode !== "settings" || pending || mutationPending) return;
    setPending(true);
    setError(undefined);
    try {
      await admin.signOut(state.session.csrf);
      setState({ mode: "sign-in" });
    } catch (failure) {
      if (failure instanceof ApiError && failure.code === "unauthorized")
        sessionExpired();
      else setError(errorLabel(failure));
    } finally {
      setPending(false);
    }
  }

  return (
    <>
      <header className="site-header">
        <div className="shell header-inner">
          <div className="brand">
            <img src={import.meta.env.BASE_URL + "assets/icon.svg"} alt="" />
            <span>{t("brand")}</span>
            <span className="brand-context">{t("localManagement")}</span>
          </div>
          <div className="header-actions">
            <label className="language-field">
              <span className="sr-only">{t("language")}</span>
              <select
                value={language}
                onChange={(event) =>
                  setLanguage(event.currentTarget.value as Language)
                }
              >
                <option value="en" lang="en">
                  English
                </option>
                <option value="zh-CN" lang="zh-CN">
                  简体中文
                </option>
              </select>
            </label>
            {state.mode === "settings" && (
              <button
                type="button"
                className="button secondary"
                disabled={pending || mutationPending}
                onClick={() => void signOut()}
              >
                {t("signOut")}
              </button>
            )}
          </div>
        </div>
      </header>
      <main className="shell main">
        {state.mode === "loading" && (
          <p className="loading" role="status">
            {t("loading")}
          </p>
        )}
        {state.mode === "sign-in" && (
          <LoginPage
            error={state.error}
            pending={pending}
            signIn={signIn}
            t={t}
          />
        )}
        {state.mode === "settings" && (
          <>
            {error && (
              <p className="message error" role="alert">
                {t(error)}
              </p>
            )}
            <SettingsPage
              csrf={state.session.csrf}
              t={t}
              onExpired={sessionExpired}
              onBusyChange={setMutationPending}
              disabled={pending}
            />
          </>
        )}
      </main>
    </>
  );
}

function LoginPage({
  pending,
  error,
  signIn,
  t,
}: {
  pending: boolean;
  error?: string;
  signIn: (event: FormEvent<HTMLFormElement>) => Promise<void>;
  t: Text;
}) {
  const [commandBefore, commandAfter] = t("signInCommand").split("{command}");
  return (
    <>
      <div className="intro">
        <h1>{t("signInTitle")}</h1>
        <p>{t("signInDescription")}</p>
      </div>
      <section className="auth-panel">
        <form className="panel-body" onSubmit={(event) => void signIn(event)}>
          {error && (
            <p className="message error" role="alert">
              {t(error)}
            </p>
          )}
          <label className="field">
            <span>{t("adminToken")}</span>
            <input
              type="password"
              name="token"
              required
              maxLength={8192}
              autoComplete="off"
              disabled={pending}
            />
          </label>
          <div className="actions">
            <button type="submit" className="button" disabled={pending}>
              {t("signIn")}
            </button>
          </div>
          <p>
            {commandBefore}
            <code>
              qcopilots-websearch --ConfigPath runtime.json --SettingsUrl
            </code>
            {commandAfter}
          </p>
        </form>
      </section>
    </>
  );
}
