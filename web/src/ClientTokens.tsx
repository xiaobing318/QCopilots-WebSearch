import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import {
  CheckCircledIcon,
  CheckIcon,
  CopyIcon,
  EyeClosedIcon,
  EyeOpenIcon,
  PlusIcon,
} from "@radix-ui/react-icons";
import { admin, ApiError, type ClientRegistryView } from "./admin";
import { errorLabel, type Text } from "./labels";

type Props = {
  csrf: string;
  t: Text;
  onExpired: () => void;
  mcpUrl?: string;
  onBusyChange?: (busy: boolean) => void;
  disabled?: boolean;
};
type Notice = { key: string; variant: "error" | "success" };
type Editor = { mode: "new" | "replace"; id: string };
type Method = "random" | "text" | "manual";
type Step = 1 | 2 | 3;
const stepKeys = ["clientStepGenerate", "clientStepSave", "clientStepCopy"];

function connectionEndpoint(value?: string) {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? value
      : undefined;
  } catch {
    return undefined;
  }
}

export function ClientTokens({
  csrf,
  t,
  onExpired,
  mcpUrl,
  onBusyChange,
  disabled = false,
}: Props) {
  const [view, setView] = useState<ClientRegistryView>();
  const [selected, setSelected] = useState<string>();
  const [notice, setNotice] = useState<Notice>();
  const [busy, setBusy] = useState(false);
  const [editor, setEditor] = useState<Editor>();
  const [step, setStep] = useState<Step>(1);
  const [method, setMethod] = useState<Method>("random");
  const [deleting, setDeleting] = useState(false);
  const [token, setToken] = useState("");
  const [sourceText, setSourceText] = useState("");
  const [generating, setGenerating] = useState(false);
  const [visible, setVisible] = useState(false);
  const [copyNotice, setCopyNotice] = useState<string>();
  const [conflict, setConflict] = useState(false);
  const id = useId();
  const operation = useRef(false);
  const generation = useRef(0);
  const focusNextInput = useRef(false);
  const locked = busy || disabled;
  const editingDisabled = locked || generating;
  const current = view?.clients.find((client) => client.id === selected);
  const connectionUrl = connectionEndpoint(mcpUrl);

  useEffect(() => {
    onBusyChange?.(busy);
    return () => onBusyChange?.(false);
  }, [busy, onBusyChange]);
  useEffect(
    () => () => {
      generation.current += 1;
    },
    [],
  );
  useEffect(() => {
    const abort = new AbortController();
    void admin.clients(abort.signal).then(
      (value) => {
        if (!abort.signal.aborted) {
          setView(value);
          setSelected(value.clients[0]?.id);
        }
      },
      (failure: unknown) => {
        if (abort.signal.aborted) return;
        if (failure instanceof ApiError && failure.code === "unauthorized")
          onExpired();
        else setNotice({ key: errorLabel(failure), variant: "error" });
      },
    );
    return () => abort.abort();
  }, [onExpired]);

  function clearDraft() {
    generation.current += 1;
    focusNextInput.current = false;
    setGenerating(false);
    setEditor(undefined);
    setToken("");
    setSourceText("");
    setVisible(false);
    setCopyNotice(undefined);
    setConflict(false);
    setNotice(undefined);
    setStep(1);
    setMethod("random");
  }
  function selectClient(value: string) {
    if (locked || operation.current) return;
    clearDraft();
    setSelected(value);
    setDeleting(false);
    setNotice(undefined);
  }
  function startEditor(value: Editor) {
    if (locked || operation.current) return;
    clearDraft();
    setEditor(value);
    setSelected(value.mode === "new" ? undefined : value.id);
    focusNextInput.current = value.mode === "new";
    setDeleting(false);
    setNotice(undefined);
  }
  function focusEditor(input: HTMLInputElement | null) {
    if (input && focusNextInput.current) {
      input.focus();
      focusNextInput.current = false;
    }
  }
  async function perform(task: () => Promise<void>) {
    if (locked || operation.current) return;
    operation.current = true;
    setBusy(true);
    setNotice(undefined);
    try {
      await task();
    } catch (failure) {
      if (failure instanceof ApiError && failure.code === "unauthorized")
        onExpired();
      else {
        if (failure instanceof ApiError && failure.code === "revision_conflict")
          setConflict(true);
        setNotice({ key: errorLabel(failure), variant: "error" });
      }
    } finally {
      operation.current = false;
      setBusy(false);
    }
  }
  function refresh() {
    if (locked || operation.current) return;
    if (step === 3) clearDraft();
    void perform(async () => {
      const value = await admin.clients();
      setView(value);
      setConflict(false);
      if (!editor && !value.clients.some((client) => client.id === selected)) {
        setSelected(value.clients[0]?.id);
        setDeleting(false);
      }
      if (
        editor?.mode === "replace" &&
        !value.clients.some((client) => client.id === editor.id)
      )
        setNotice({ key: "clientNoLongerExists", variant: "error" });
    });
  }
  function chooseMethod(value: Method) {
    if (editingDisabled) return;
    generation.current += 1;
    setMethod(value);
    setToken("");
    setSourceText("");
    setVisible(false);
    setCopyNotice(undefined);
    setNotice(undefined);
  }
  function generateRandom() {
    if (editingDisabled) return;
    try {
      const bytes = crypto.getRandomValues(new Uint8Array(32));
      setToken(
        Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join(
          "",
        ),
      );
      setSourceText("");
      setVisible(false);
      setNotice(undefined);
    } catch {
      setNotice({ key: "clientGenerateFailed", variant: "error" });
    }
  }
  async function generateFromText() {
    if (editingDisabled || sourceText.length === 0) return;
    const requestedGeneration = ++generation.current;
    setGenerating(true);
    setNotice(undefined);
    try {
      const digest = await crypto.subtle.digest(
        "SHA-256",
        new TextEncoder().encode(sourceText),
      );
      if (generation.current !== requestedGeneration) return;
      setToken(
        Array.from(new Uint8Array(digest), (value) =>
          value.toString(16).padStart(2, "0"),
        ).join(""),
      );
      setSourceText("");
      setVisible(false);
    } catch {
      if (generation.current === requestedGeneration)
        setNotice({ key: "clientGenerateFailed", variant: "error" });
    } finally {
      if (generation.current === requestedGeneration) setGenerating(false);
    }
  }
  function review(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!view || !editor || editingDisabled) return;
    if (!/^[A-Za-z0-9_.-]{1,64}$/.test(editor.id)) {
      setNotice({ key: "clientInvalidId", variant: "error" });
      return;
    }
    if (!/^[\x21-\x7e]{1,8192}$/.test(token)) {
      setNotice({ key: "clientInvalidToken", variant: "error" });
      return;
    }
    setSourceText("");
    setVisible(false);
    setNotice(undefined);
    setStep(2);
  }
  function save() {
    if (!view || !editor || step !== 2 || conflict) return;
    void perform(async () => {
      const update =
        editor.mode === "new" ? admin.addClient : admin.replaceClient;
      const value = await update(view.revision, editor.id, token, csrf);
      setView(value);
      setSelected(editor.id);
      setSourceText("");
      setVisible(false);
      setStep(3);
    });
  }
  function remove() {
    if (!view || !current || conflict) return;
    void perform(async () => {
      const value = await admin.deleteClient(view.revision, current.id, csrf);
      setView(value);
      clearDraft();
      setSelected(value.clients[0]?.id);
      setDeleting(false);
      setNotice({ key: "clientDeleted", variant: "success" });
    });
  }
  async function copy(value: string, message: string) {
    const requestedGeneration = generation.current;
    try {
      await navigator.clipboard.writeText(value);
      if (requestedGeneration === generation.current) setCopyNotice(message);
    } catch {
      if (requestedGeneration === generation.current) {
        setCopyNotice("clientCopyFailed");
        setVisible(true);
      }
    }
  }
  const missingEditedClient =
    editor?.mode === "replace" &&
    !view?.clients.some((client) => client.id === editor.id);
  function visibilityButton() {
    return (
      <button
        type="button"
        className="icon-button"
        disabled={editingDisabled || !token}
        aria-label={t(visible ? "clientHide" : "clientShow")}
        onClick={() => setVisible(!visible)}
      >
        {visible ? <EyeClosedIcon /> : <EyeOpenIcon />}
      </button>
    );
  }

  return (
    <div className="authorization-layout">
      <aside className="client-sidebar" aria-labelledby={id + "-clients"}>
        <div className="client-sidebar-heading">
          <h2 id={id + "-clients"}>{t("clientListTitle")}</h2>
          <button
            type="button"
            className="button outlined"
            disabled={locked || !view}
            onClick={() => startEditor({ mode: "new", id: "" })}
          >
            <PlusIcon aria-hidden="true" />
            {t("clientAdd")}
          </button>
        </div>
        {view ? (
          view.clients.length ? (
            <ul className="client-list" aria-label={t("clientListTitle")}>
              {view.clients.map((client) => (
                <li key={client.id}>
                  <button
                    type="button"
                    className={
                      "client-select" +
                      (selected === client.id ? " selected" : "")
                    }
                    aria-current={selected === client.id ? true : undefined}
                    aria-label={t("clientSelectNamed", { id: client.id })}
                    disabled={locked}
                    onClick={() => selectClient(client.id)}
                  >
                    <strong>{client.id}</strong>
                    <span>{t("clientSource." + client.source)}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="key-state sidebar-empty">{t("clientsEmpty")}</p>
          )
        ) : (
          <p className="key-state sidebar-empty" role="status">
            {t(notice ? "clientsLoadFailed" : "clientsLoading")}
          </p>
        )}
        <button
          type="button"
          className="text-button sidebar-refresh"
          disabled={locked}
          onClick={refresh}
        >
          {t("clientsRefresh")}
        </button>
      </aside>
      <section className="client-detail" aria-labelledby={id + "-detail"}>
        <h2 id={id + "-detail"}>
          {editor
            ? t(
                editor.mode === "new" ? "clientNewTitle" : "clientReplaceTitle",
                { id: editor.id },
              )
            : (current?.id ?? t("clientWelcomeTitle"))}
        </h2>
        {notice && (
          <p
            className={"message " + notice.variant}
            role={notice.variant === "error" ? "alert" : "status"}
          >
            {t(notice.key)}
          </p>
        )}
        {view?.pending_restart && (
          <p className="message" role="status">
            {t("clientsPendingRestart")}
          </p>
        )}
        {conflict && (
          <div className="conflict-actions">
            <p>{t("clientConflictHelp")}</p>
            <button
              type="button"
              className="button secondary"
              disabled={locked}
              onClick={refresh}
            >
              {t("clientReload")}
            </button>
          </div>
        )}
        {editor ? (
          <>
            <ol className="workflow-steps" aria-label={t("clientWorkflow")}>
              {stepKeys.map((key, index) => (
                <li
                  key={key}
                  className={
                    index + 1 === step
                      ? "current"
                      : index + 1 < step
                        ? "complete"
                        : ""
                  }
                  aria-current={index + 1 === step ? "step" : undefined}
                >
                  <span className="step-number" aria-hidden="true">
                    {index + 1 < step ? <CheckIcon /> : index + 1}
                  </span>
                  <span>{t(key)}</span>
                </li>
              ))}
            </ol>
            {step === 1 && (
              <form
                className="guided-form"
                onSubmit={review}
                noValidate
                aria-label={t(
                  editor.mode === "new" ? "clientAdd" : "clientReplace",
                )}
              >
                {editor.mode === "new" && (
                  <div className="generation-row">
                    <label htmlFor={id + "-client-id"}>{t("clientId")}</label>
                    <div className="field">
                      <input
                        ref={focusEditor}
                        id={id + "-client-id"}
                        value={editor.id}
                        maxLength={64}
                        autoComplete="off"
                        spellCheck={false}
                        disabled={editingDisabled}
                        aria-describedby={id + "-id-help"}
                        onChange={(event) =>
                          setEditor({
                            ...editor,
                            id: event.currentTarget.value,
                          })
                        }
                      />
                      <small id={id + "-id-help"}>{t("clientIdHelp")}</small>
                    </div>
                  </div>
                )}
                <div className="generation-row">
                  <span className="field-label" id={id + "-method"}>
                    {t("clientGenerationMethod")}
                  </span>
                  <div
                    className="segmented"
                    role="group"
                    aria-labelledby={id + "-method"}
                  >
                    <button
                      type="button"
                      disabled={editingDisabled}
                      aria-pressed={method === "random"}
                      onClick={() => chooseMethod("random")}
                    >
                      {t("clientRandomMethod")}
                    </button>
                    <button
                      type="button"
                      disabled={editingDisabled}
                      aria-pressed={method === "text"}
                      onClick={() => chooseMethod("text")}
                    >
                      {t("clientGenerateText")}
                    </button>
                  </div>
                </div>
                {method === "random" && (
                  <div className="generation-row">
                    <span className="field-label">{t("clientRandomCode")}</span>
                    <div className="field">
                      <div>
                        <button
                          type="button"
                          className="button outlined"
                          disabled={editingDisabled}
                          onClick={generateRandom}
                        >
                          {t("clientGenerate")}
                        </button>
                      </div>
                      <small>{t("clientRandomHelp")}</small>
                    </div>
                  </div>
                )}
                {method === "text" && (
                  <div className="generation-row">
                    <label htmlFor={id + "-source-text"}>
                      {t("clientSourceText")}
                    </label>
                    <div className="field">
                      <div className="source-inline">
                        <textarea
                          id={id + "-source-text"}
                          rows={1}
                          value={sourceText}
                          autoComplete="off"
                          autoCapitalize="none"
                          spellCheck={false}
                          disabled={editingDisabled}
                          aria-describedby={id + "-source-help"}
                          onChange={(event) => {
                            setSourceText(event.currentTarget.value);
                            setToken("");
                            setVisible(false);
                            setCopyNotice(undefined);
                          }}
                        />
                        <button
                          type="button"
                          className="button secondary"
                          disabled={editingDisabled || sourceText.length === 0}
                          onClick={() => void generateFromText()}
                        >
                          {t(
                            generating
                              ? "clientGeneratingText"
                              : "clientGenerateCode",
                          )}
                        </button>
                      </div>
                      <small id={id + "-source-help"}>
                        {t("clientSourceTextHelp")}
                      </small>
                    </div>
                  </div>
                )}
                <div className="token-field field">
                  <label htmlFor={id + "-token"}>{t("clientToken")}</label>
                  <div className="secret-input">
                    <input
                      id={id + "-token"}
                      type={visible ? "text" : "password"}
                      value={token}
                      maxLength={8192}
                      readOnly={method !== "manual"}
                      autoComplete="new-password"
                      spellCheck={false}
                      autoCapitalize="none"
                      disabled={editingDisabled}
                      onChange={(event) => setToken(event.currentTarget.value)}
                    />
                    {visibilityButton()}
                  </div>
                  <div className="token-field-footer">
                    <p
                      className={"draft-state" + (token ? " ready" : "")}
                      role="status"
                    >
                      {token && <CheckCircledIcon aria-hidden="true" />}
                      {t(token ? "clientDraftReady" : "clientDraftEmpty")}
                    </p>
                    <button
                      type="button"
                      className="text-button"
                      disabled={editingDisabled}
                      aria-pressed={method === "manual"}
                      onClick={() => chooseMethod("manual")}
                    >
                      {t("clientManual")}
                    </button>
                  </div>
                  {method === "manual" && <small>{t("clientTokenHelp")}</small>}
                </div>
                <div className="workflow-primary">
                  <div className="actions">
                    <button
                      type="submit"
                      className="button primary-action"
                      disabled={editingDisabled || !token}
                    >
                      {t("clientNext")}
                    </button>
                    <button
                      type="button"
                      className="button secondary"
                      disabled={locked}
                      onClick={clearDraft}
                    >
                      {t("cancel")}
                    </button>
                  </div>
                  <p className="key-state">
                    {t(
                      editor.mode === "replace"
                        ? "clientReplaceBeforeSave"
                        : "clientBeforeSave",
                    )}
                  </p>
                </div>
              </form>
            )}
            {step === 2 && (
              <div className="review-step">
                <p>
                  {t(
                    editor.mode === "replace"
                      ? "clientReviewReplace"
                      : "clientReviewNew",
                    { id: editor.id },
                  )}
                </p>
                <dl className="review-summary">
                  <div>
                    <dt>{t("clientId")}</dt>
                    <dd>{editor.id}</dd>
                  </div>
                  <div>
                    <dt>{t("clientReviewCode")}</dt>
                    <dd>
                      {t("clientReviewCodeLength", { count: token.length })}
                    </dd>
                  </div>
                </dl>
                <p className="message">{t("clientSaveEffect")}</p>
                <div className="actions">
                  <button
                    type="button"
                    className="button primary-action"
                    disabled={locked || conflict || missingEditedClient}
                    onClick={save}
                  >
                    {t(busy ? "clientSaving" : "clientSave")}
                  </button>
                  <button
                    type="button"
                    className="button secondary"
                    disabled={locked}
                    onClick={() => setStep(1)}
                  >
                    {t("clientBack")}
                  </button>
                  <button
                    type="button"
                    className="text-button"
                    disabled={locked}
                    onClick={clearDraft}
                  >
                    {t("cancel")}
                  </button>
                </div>
              </div>
            )}
            {step === 3 && (
              <div className="connection-step">
                <p className="activation-success" role="status">
                  <CheckCircledIcon aria-hidden="true" />
                  {t("clientActive")}
                </p>
                <p>{t("clientConnectHelp")}</p>
                <div className="field">
                  <label htmlFor={id + "-url"}>{t("clientMcpUrl")}</label>
                  <div className="copy-field">
                    <input
                      id={id + "-url"}
                      value={connectionUrl ?? ""}
                      readOnly
                      spellCheck={false}
                    />
                    <button
                      type="button"
                      className="button secondary"
                      disabled={!connectionUrl || locked}
                      onClick={() =>
                        connectionUrl &&
                        void copy(connectionUrl, "clientUrlCopied")
                      }
                    >
                      <CopyIcon aria-hidden="true" />
                      {t("clientCopyUrl")}
                    </button>
                  </div>
                  {!connectionUrl && <small>{t("clientUrlUnavailable")}</small>}
                </div>
                <div className="field">
                  <label htmlFor={id + "-saved-token"}>
                    {t("clientToken")}
                  </label>
                  <div className="copy-field">
                    <div className="secret-input">
                      <input
                        id={id + "-saved-token"}
                        type={visible ? "text" : "password"}
                        value={token}
                        readOnly
                        autoComplete="off"
                      />
                      {visibilityButton()}
                    </div>
                    <button
                      type="button"
                      className="button secondary"
                      disabled={locked}
                      onClick={() => void copy(token, "clientCopied")}
                    >
                      <CopyIcon aria-hidden="true" />
                      {t("clientCopy")}
                    </button>
                  </div>
                </div>
                {copyNotice && (
                  <p className="key-state" role="status">
                    {t(copyNotice)}
                  </p>
                )}
                <p className="message">{t("clientOneTimeCopy")}</p>
                <div>
                  <button
                    type="button"
                    className="button primary-action"
                    disabled={locked}
                    onClick={clearDraft}
                  >
                    {t("clientDone")}
                  </button>
                </div>
              </div>
            )}
            {step === 1 && (
              <div className="workflow-preview">
                <div>
                  <span className="step-number" aria-hidden="true">
                    2
                  </span>
                  <strong>{t("clientStepSave")}</strong>
                  <p>{t("clientSavePreview")}</p>
                </div>
                <div>
                  <span className="step-number" aria-hidden="true">
                    3
                  </span>
                  <strong>{t("clientStepCopy")}</strong>
                  <p>{t("clientCopyPreview")}</p>
                </div>
              </div>
            )}
          </>
        ) : current ? (
          <div className="existing-client">
            <p>{t("clientExistingHelp")}</p>
            <dl className="review-summary">
              <div>
                <dt>{t("clientId")}</dt>
                <dd>{current.id}</dd>
              </div>
              <div>
                <dt>{t("clientSourceLabel")}</dt>
                <dd>{t("clientSource." + current.source)}</dd>
              </div>
              <div>
                <dt>{t("clientToken")}</dt>
                <dd>{t("clientStoredHidden")}</dd>
              </div>
            </dl>
            {deleting ? (
              <div
                className="revoke-confirm"
                role="group"
                aria-label={t("clientDelete")}
              >
                <h3>{t("clientRevokeTitle")}</h3>
                <p>{t("clientDeleteConfirm", { id: current.id })}</p>
                <div className="actions">
                  <button
                    type="button"
                    className="button danger"
                    disabled={locked || conflict}
                    onClick={remove}
                  >
                    {t("clientConfirmDelete")}
                  </button>
                  <button
                    type="button"
                    className="button secondary"
                    disabled={locked}
                    onClick={() => setDeleting(false)}
                  >
                    {t("cancel")}
                  </button>
                </div>
              </div>
            ) : (
              <div className="actions">
                <button
                  type="button"
                  className="button"
                  disabled={locked}
                  aria-label={t("clientReplaceNamed", { id: current.id })}
                  onClick={() =>
                    startEditor({ mode: "replace", id: current.id })
                  }
                >
                  {t("clientReplace")}
                </button>
                <button
                  type="button"
                  className="text-button danger-text"
                  disabled={locked}
                  aria-label={t("clientDeleteNamed", { id: current.id })}
                  onClick={() => {
                    clearDraft();
                    setDeleting(true);
                    setNotice(undefined);
                  }}
                >
                  {t("clientDelete")}
                </button>
              </div>
            )}
          </div>
        ) : (
          <div className="existing-client">
            <p>{t("clientWelcomeHelp")}</p>
            <div>
              <button
                type="button"
                className="button"
                disabled={locked || !view}
                onClick={() => startEditor({ mode: "new", id: "" })}
              >
                {t("clientStart")}
              </button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
