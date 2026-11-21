import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { webcrypto } from "node:crypto";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { type ClientRegistryView } from "../admin";
import { ClientTokens } from "../ClientTokens";
import { localized } from "../labels";

const csrf = "client-session-csrf";
const mcpUrl = "https://web-search.vastarmorai.com/mcp";
const initial: ClientRegistryView = {
  revision: "clients-1",
  clients: [{ id: "local", source: "file", can_reveal: true }],
  pending_restart: false,
};

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function service(value = initial, { active = true } = {}) {
  let view = structuredClone(value);
  const savedTokens = new Map(
    value.clients.map((client) => [client.id, "stored-code-for-" + client.id]),
  );
  let nextError: string | undefined;
  let nextPending: Promise<Response> | undefined;
  let revision = 1;
  const fetcher = vi.fn((path: string, options: RequestInit) => {
    if (
      path !== "/admin/api/v1/clients" &&
      path !== "/admin/api/v1/clients/reveal"
    )
      return json({ error: { code: "not_found" } }, 404);
    if (options.method === "GET") return json(view);
    if (nextPending) {
      const pending = nextPending;
      nextPending = undefined;
      return pending;
    }
    if (nextError) {
      const error = nextError;
      nextError = undefined;
      if (error === "revision_conflict") {
        view.revision = "clients-other-session";
        view.clients.push({ id: "other-client", source: "environment" });
      }
      return json(
        { error: { code: error } },
        error === "unauthorized" ? 401 : error === "internal_error" ? 500 : 409,
      );
    }
    const body = JSON.parse(String(options.body));
    if (path === "/admin/api/v1/clients/reveal")
      return json({
        id: body.id,
        token: savedTokens.get(body.id),
        revision: view.revision,
        active,
      });
    if (options.method === "POST") {
      view.clients.push({ id: body.id, source: "manual", can_reveal: true });
      savedTokens.set(body.id, body.token);
    } else {
      const id = body.id;
      if (options.method === "DELETE") {
        view.clients = view.clients.filter((client) => client.id !== id);
        savedTokens.delete(id);
      } else {
        view.clients = view.clients.map((client) =>
          client.id === id
            ? { id, source: "manual", can_reveal: true }
            : client,
        );
        savedTokens.set(id, body.token);
      }
    }
    view = {
      ...view,
      revision: "clients-" + ++revision,
      pending_restart: false,
    };
    return json(view);
  });
  vi.stubGlobal("fetch", fetcher);
  return {
    fetcher,
    failNext: (code: string) => {
      nextError = code;
    },
    deferNext: (pending: Promise<Response>) => {
      nextPending = pending;
    },
  };
}

function show(language: "en" | "zh-CN" = "en") {
  const onExpired = vi.fn();
  const onBusyChange = vi.fn();
  render(
    <ClientTokens
      csrf={csrf}
      t={localized(language)}
      onExpired={onExpired}
      mcpUrl={mcpUrl}
      onBusyChange={onBusyChange}
    />,
  );
  return { onExpired, onBusyChange };
}

async function replaceManually(
  user: ReturnType<typeof userEvent.setup>,
  value: string,
) {
  await user.click(
    await screen.findByRole("button", { name: "Replace code for local" }),
  );
  await user.click(screen.getByRole("button", { name: "Enter manually" }));
  fireEvent.change(screen.getByLabelText("Bearer authorization code"), {
    target: { value },
  });
}

async function reviewAndSave(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Next: review save" }));
  await user.click(screen.getByRole("button", { name: "Save and activate" }));
}

it("adds a short slash-containing code only after review, then copies the active connection details", async () => {
  const user = userEvent.setup();
  const clipboard = vi
    .spyOn(navigator.clipboard, "writeText")
    .mockResolvedValue();
  const { fetcher } = service();
  show();
  await user.click(await screen.findByRole("button", { name: "Add client" }));
  await user.type(screen.getByLabelText("Client name"), "qgis-client");
  await user.click(screen.getByRole("button", { name: "Enter manually" }));
  const token = screen.getByLabelText("Bearer authorization code");
  expect(token).toHaveAttribute("type", "password");
  await user.type(token, "xbyang/qcopilots-websearch");
  await user.click(screen.getByRole("button", { name: "Next: review save" }));
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("button", { name: "Copy Bearer" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Save and activate" }));
  expect(
    await screen.findByRole("button", { name: "Copy Bearer" }),
  ).toBeEnabled();
  expect(fetcher).toHaveBeenCalledWith(
    "/admin/api/v1/clients",
    expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({ "X-QCWS-CSRF": csrf }),
      credentials: "same-origin",
      redirect: "error",
      body: JSON.stringify({
        revision: "clients-1",
        id: "qgis-client",
        token: "xbyang/qcopilots-websearch",
      }),
    }),
  );
  await user.click(screen.getByRole("button", { name: "Copy Bearer" }));
  expect(clipboard).toHaveBeenLastCalledWith("xbyang/qcopilots-websearch");
  await user.click(screen.getByRole("button", { name: "Copy server URL" }));
  expect(clipboard).toHaveBeenLastCalledWith(mcpUrl);
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(0);
  await user.click(screen.getByRole("button", { name: "Done" }));
  expect(screen.queryByRole("button", { name: "Copy Bearer" })).toBeNull();
  expect(screen.queryByDisplayValue("xbyang/qcopilots-websearch")).toBeNull();
  await user.click(
    screen.getByRole("button", { name: "Replace code for qgis-client" }),
  );
  expect(screen.getByLabelText("Bearer authorization code")).toHaveValue("");
});

it("replaces an imported code without renaming the client or retrieving its old code", async () => {
  const user = userEvent.setup();
  const { fetcher } = service();
  show();
  await replaceManually(user, "x");
  expect(screen.queryByLabelText("Client name")).toBeNull();
  await reviewAndSave(user);
  await screen.findByRole("button", { name: "Copy Bearer" });
  expect(fetcher).toHaveBeenCalledWith(
    "/admin/api/v1/clients",
    expect.objectContaining({
      method: "PUT",
      headers: expect.objectContaining({ "X-QCWS-CSRF": csrf }),
      body: JSON.stringify({ revision: "clients-1", id: "local", token: "x" }),
    }),
  );
});

it("confirms revocation and leaves an empty client list usable", async () => {
  const user = userEvent.setup();
  const { fetcher } = service();
  show();
  await user.click(
    await screen.findByRole("button", { name: "Revoke access for local" }),
  );
  expect(fetcher).toHaveBeenCalledTimes(1);
  await user.click(
    screen.getByRole("button", { name: "Revoke authorization code" }),
  );
  expect(await screen.findByText(/No client is authorized/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Add client" })).toBeEnabled();
  expect(fetcher).toHaveBeenCalledWith(
    "/admin/api/v1/clients",
    expect.objectContaining({
      method: "DELETE",
      headers: expect.objectContaining({ "X-QCWS-CSRF": csrf }),
      body: JSON.stringify({ revision: "clients-1", id: "local" }),
    }),
  );
  await user.click(screen.getByRole("button", { name: "Add client" }));
  expect(screen.getByLabelText("Client name")).toBeEnabled();
});

it("generates 32 random bytes without activating them and forgets the draft on cancel", async () => {
  const user = userEvent.setup();
  const random = vi.spyOn(crypto, "getRandomValues");
  const { fetcher } = service();
  show();
  await user.click(await screen.findByRole("button", { name: "Add client" }));
  await user.click(
    screen.getByRole("button", { name: "Generate random code" }),
  );
  expect(random).toHaveBeenCalledWith(expect.any(Uint8Array));
  expect((random.mock.calls[0][0] as Uint8Array).length).toBe(32);
  const input = screen.getByLabelText<HTMLInputElement>(
    "Bearer authorization code",
  );
  expect(input.value).toMatch(/^[0-9a-f]{64}$/);
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("button", { name: "Copy Bearer" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  await user.click(screen.getByRole("button", { name: "Add client" }));
  expect(screen.getByLabelText("Bearer authorization code")).toHaveValue("");
});

it.each([
  ["abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"],
  [" abc ", "3eaf1941003943dfaa935adecffcaaa217e290def6fb0181141ced6c9daabaad"],
  [
    "中文🙂\n qcopilots ",
    "7123d19d4b8d1b563fc791f469a0c9cdaca836f28e94fb9a96232b1ff87d9f4e",
  ],
])(
  "derives the same 64-hex code from exact UTF-8 text and submits only the derived code (%#)",
  async (text, expected) => {
    vi.stubGlobal("crypto", webcrypto);
    const user = userEvent.setup();
    const { fetcher } = service();
    show();
    await user.click(
      await screen.findByRole("button", { name: "Replace code for local" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Generate from text" }),
    );
    const source = screen.getByLabelText("Text to generate from");
    const token = screen.getByLabelText("Bearer authorization code");
    for (let attempt = 0; attempt < 2; attempt += 1) {
      fireEvent.change(source, { target: { value: text } });
      await user.click(screen.getByRole("button", { name: "Generate code" }));
      await waitFor(() => expect(token).toHaveValue(expected));
      expect(source).toHaveValue("");
    }
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    await reviewAndSave(user);
    await screen.findByRole("button", { name: "Copy Bearer" });
    expect(fetcher).toHaveBeenLastCalledWith(
      "/admin/api/v1/clients",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          revision: "clients-1",
          id: "local",
          token: expected,
        }),
      }),
    );
    await user.click(screen.getByRole("button", { name: "Done" }));
    await user.click(
      screen.getByRole("button", { name: "Replace code for local" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Generate from text" }),
    );
    expect(screen.getByLabelText("Text to generate from")).toHaveValue("");
    expect(screen.getByLabelText("Bearer authorization code")).toHaveValue("");
  },
);

it("invalidates the derived code when its source text is edited and requires generation again", async () => {
  vi.stubGlobal("crypto", webcrypto);
  const user = userEvent.setup();
  const { fetcher } = service();
  show();
  await user.click(
    await screen.findByRole("button", { name: "Replace code for local" }),
  );
  await user.click(screen.getByRole("button", { name: "Generate from text" }));
  const source = screen.getByLabelText("Text to generate from");
  const token = screen.getByLabelText("Bearer authorization code");
  const next = screen.getByRole("button", { name: "Next: review save" });

  await user.type(source, "abc");
  await user.click(screen.getByRole("button", { name: "Generate code" }));
  await waitFor(() =>
    expect(token).toHaveValue(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    ),
  );
  expect(next).toBeEnabled();
  expect(source).toHaveValue("");

  await user.type(source, " abc ");
  expect(token).toHaveValue("");
  expect(next).toBeDisabled();
  await user.click(next);
  expect(
    screen.queryByRole("button", { name: "Save and activate" }),
  ).toBeNull();
  expect(fetcher).toHaveBeenCalledTimes(1);

  await user.click(screen.getByRole("button", { name: "Generate code" }));
  await waitFor(() =>
    expect(token).toHaveValue(
      "3eaf1941003943dfaa935adecffcaaa217e290def6fb0181141ced6c9daabaad",
    ),
  );
  expect(next).toBeEnabled();
  expect(source).toHaveValue("");
  await user.click(next);
  expect(
    screen.getByRole("button", { name: "Save and activate" }),
  ).toBeEnabled();
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it("does not let a cancelled text calculation overwrite a new draft", async () => {
  let finish!: (value: ArrayBuffer) => void;
  const pending = new Promise<ArrayBuffer>((resolve) => {
    finish = resolve;
  });
  vi.stubGlobal("crypto", { subtle: { digest: vi.fn(() => pending) } });
  const user = userEvent.setup();
  service();
  show();
  await user.click(
    await screen.findByRole("button", { name: "Replace code for local" }),
  );
  await user.click(screen.getByRole("button", { name: "Generate from text" }));
  await user.type(
    screen.getByLabelText("Text to generate from"),
    "cancelled-draft",
  );
  await user.click(screen.getByRole("button", { name: "Generate code" }));
  expect(
    screen.getByRole("button", { name: "Next: review save" }),
  ).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  await replaceManually(user, "manual-replacement");
  await act(async () => {
    finish(new ArrayBuffer(32));
    await pending;
  });
  expect(screen.getByLabelText("Bearer authorization code")).toHaveValue(
    "manual-replacement",
  );
});

it("keeps a failed text calculation local and allows retrying", async () => {
  vi.stubGlobal("crypto", {
    subtle: {
      digest: vi.fn().mockRejectedValue(new Error("digest unavailable")),
    },
  });
  const user = userEvent.setup();
  const { fetcher } = service();
  show();
  await user.click(
    await screen.findByRole("button", { name: "Replace code for local" }),
  );
  await user.click(screen.getByRole("button", { name: "Generate from text" }));
  await user.type(
    screen.getByLabelText("Text to generate from"),
    "retry-this-text",
  );
  await user.click(screen.getByRole("button", { name: "Generate code" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/generate/i);
  expect(screen.getByLabelText("Bearer authorization code")).toHaveValue("");
  expect(screen.getByLabelText("Text to generate from")).toHaveValue(
    "retry-this-text",
  );
  expect(screen.getByRole("button", { name: "Generate code" })).toBeEnabled();
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it.each([" has-space", "two words", "中文", "x".repeat(8193)])(
  "rejects invalid codes without trimming or sending them (%#)",
  async (value) => {
    const user = userEvent.setup();
    const { fetcher } = service();
    show();
    await replaceManually(user, value);
    await user.click(screen.getByRole("button", { name: "Next: review save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The code is saved exactly as entered.",
    );
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("Bearer authorization code")).toHaveValue(
      value,
    );
  },
);

it("preserves a conflicting draft and requires an explicit reload before retrying", async () => {
  const user = userEvent.setup();
  const { fetcher, failNext } = service();
  show();
  await replaceManually(user, "qcopilots-websearch");
  failNext("revision_conflict");
  await reviewAndSave(user);
  expect(await screen.findByRole("alert")).toHaveTextContent(/another session/);
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(
    screen.queryByRole("button", { name: "Select client other-client" }),
  ).toBeNull();
  expect(
    screen.getByRole("button", { name: "Save and activate" }),
  ).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Reload clients" }));
  expect(
    await screen.findByRole("button", { name: "Select client other-client" }),
  ).toBeVisible();
  expect(fetcher).toHaveBeenCalledTimes(3);
  await user.click(screen.getByRole("button", { name: "Save and activate" }));
  await screen.findByRole("button", { name: "Copy Bearer" });
  expect(fetcher).toHaveBeenLastCalledWith(
    "/admin/api/v1/clients",
    expect.objectContaining({
      body: JSON.stringify({
        revision: "clients-other-session",
        id: "local",
        token: "qcopilots-websearch",
      }),
    }),
  );
});

it("clears a saved temporary code and an unsaved source when another client is selected", async () => {
  const user = userEvent.setup();
  service({
    ...initial,
    clients: [...initial.clients, { id: "second", source: "manual" }],
  });
  show();
  await replaceManually(user, "only-in-current-memory");
  await reviewAndSave(user);
  await screen.findByRole("button", { name: "Copy Bearer" });
  await user.click(
    screen.getByRole("button", { name: "Select client second" }),
  );
  expect(screen.queryByDisplayValue("only-in-current-memory")).toBeNull();
  expect(screen.queryByRole("button", { name: "Copy Bearer" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Select client local" }));
  await user.click(
    screen.getByRole("button", { name: "Replace code for local" }),
  );
  await user.click(screen.getByRole("button", { name: "Generate from text" }));
  await user.type(
    screen.getByLabelText("Text to generate from"),
    "unsaved-source",
  );
  await user.click(
    screen.getByRole("button", { name: "Select client second" }),
  );
  await user.click(screen.getByRole("button", { name: "Select client local" }));
  await user.click(
    screen.getByRole("button", { name: "Replace code for local" }),
  );
  await user.click(screen.getByRole("button", { name: "Generate from text" }));
  expect(screen.getByLabelText("Text to generate from")).toHaveValue("");
  expect(screen.getByLabelText("Bearer authorization code")).toHaveValue("");
});

it("locks client navigation until a pending save is resolved", async () => {
  const user = userEvent.setup();
  let finish!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => {
    finish = resolve;
  });
  const { deferNext } = service();
  const { onBusyChange } = show();
  await replaceManually(user, "pending-code");
  deferNext(pending);
  await reviewAndSave(user);
  expect(
    screen.getByRole("button", { name: "Select client local" }),
  ).toBeDisabled();
  expect(screen.getByRole("button", { name: "Add client" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  expect(onBusyChange).toHaveBeenLastCalledWith(true);
  await act(async () => {
    finish(json({ ...initial, revision: "clients-2" }));
    await pending;
  });
  expect(
    await screen.findByRole("button", { name: "Copy Bearer" }),
  ).toBeEnabled();
  expect(onBusyChange).toHaveBeenLastCalledWith(false);
});

it("shows pending changes and credential sources in Chinese without exposing credentials", async () => {
  service({
    ...initial,
    pending_restart: true,
    clients: [
      { id: "local", source: "file" },
      { id: "operator", source: "manual" },
      { id: "automation", source: "environment" },
    ],
  });
  show("zh-CN");
  expect(
    await screen.findByText(/部分已保存的客户端更改尚未生效/),
  ).toBeVisible();
  expect(screen.getAllByText("文件配置")[0]).toBeVisible();
  expect(screen.getAllByText("手动设置")[0]).toBeVisible();
  expect(screen.getAllByText("环境变量")[0]).toBeVisible();
  expect(screen.queryByLabelText("Bearer 授权码")).toBeNull();
});

it("expires the management session without treating a rejected write as saved", async () => {
  const user = userEvent.setup();
  const { failNext } = service();
  const { onExpired } = show();
  await replaceManually(user, "new-code");
  failNext("unauthorized");
  await reviewAndSave(user);
  await waitFor(() => expect(onExpired).toHaveBeenCalledOnce());
  expect(screen.queryByRole("button", { name: "Copy Bearer" })).toBeNull();
});

it("preserves the draft after a failed write and never presents it as active", async () => {
  const user = userEvent.setup();
  const { fetcher, failNext } = service();
  show();
  await replaceManually(user, "retry-after-write-error");
  failNext("internal_error");
  await reviewAndSave(user);
  expect(await screen.findByRole("alert")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Copy Bearer" })).toBeNull();
  expect(screen.queryByText("Saved and active")).toBeNull();
  expect(fetcher).toHaveBeenCalledTimes(2);
  await user.click(screen.getByRole("button", { name: "Save and activate" }));
  expect(
    await screen.findByRole("button", { name: "Copy Bearer" }),
  ).toBeEnabled();
  expect(fetcher).toHaveBeenLastCalledWith(
    "/admin/api/v1/clients",
    expect.objectContaining({
      body: JSON.stringify({
        revision: "clients-1",
        id: "local",
        token: "retry-after-write-error",
      }),
    }),
  );
});

it("accepts the 8192-character boundary without truncating the saved code", async () => {
  const user = userEvent.setup();
  const { fetcher } = service();
  show();
  const code = "/".repeat(8192);
  await replaceManually(user, code);
  await reviewAndSave(user);
  await screen.findByRole("button", { name: "Copy Bearer" });
  expect(fetcher).toHaveBeenLastCalledWith(
    "/admin/api/v1/clients",
    expect.objectContaining({
      body: JSON.stringify({ revision: "clients-1", id: "local", token: code }),
    }),
  );
});

it("keeps server URL copying unavailable when status has not supplied an endpoint", async () => {
  const user = userEvent.setup();
  service();
  render(<ClientTokens csrf={csrf} t={localized("en")} onExpired={vi.fn()} />);
  await replaceManually(user, "valid-code");
  await reviewAndSave(user);
  expect(
    await screen.findByRole("button", { name: "Copy Bearer" }),
  ).toBeEnabled();
  expect(
    screen.getByRole("button", { name: "Copy server URL" }),
  ).toBeDisabled();
  expect(screen.getByText(/The server URL is unavailable/)).toBeVisible();
  expect(document.body).not.toHaveTextContent("web-search.vastarmorai.com");
});

it("clears the temporary connection code before refreshing client state", async () => {
  const user = userEvent.setup();
  service();
  show();
  await replaceManually(user, "possibly-rotated-elsewhere");
  await reviewAndSave(user);
  await screen.findByRole("button", { name: "Copy Bearer" });
  await user.click(screen.getByRole("button", { name: "Refresh clients" }));
  await screen.findByRole("button", { name: "Replace code for local" });
  expect(screen.queryByText("Saved and active")).toBeNull();
  expect(screen.queryByDisplayValue("possibly-rotated-elsewhere")).toBeNull();
  expect(screen.queryByRole("button", { name: "Copy Bearer" })).toBeNull();
  await user.click(
    screen.getByRole("button", { name: "Replace code for local" }),
  );
  expect(screen.getByLabelText("Bearer authorization code")).toHaveValue("");
});

it.each([".", ".."])(
  "creates, replaces and revokes client %s without placing its ID in the URL path",
  async (id) => {
    const user = userEvent.setup();
    const { fetcher } = service({ ...initial, clients: [] });
    show();
    await user.click(await screen.findByRole("button", { name: "Add client" }));
    await user.type(screen.getByLabelText("Client name"), id);
    await user.click(screen.getByRole("button", { name: "Enter manually" }));
    await user.type(
      screen.getByLabelText("Bearer authorization code"),
      "first-code",
    );
    await reviewAndSave(user);
    await screen.findByRole("button", { name: "Copy Bearer" });
    expect(fetcher).toHaveBeenLastCalledWith(
      "/admin/api/v1/clients",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ "X-QCWS-CSRF": csrf }),
        body: JSON.stringify({
          revision: "clients-1",
          id,
          token: "first-code",
        }),
      }),
    );

    await user.click(screen.getByRole("button", { name: "Done" }));
    await user.click(
      screen.getByRole("button", { name: "Replace code for " + id }),
    );
    await user.click(screen.getByRole("button", { name: "Enter manually" }));
    await user.type(
      screen.getByLabelText("Bearer authorization code"),
      "second-code",
    );
    await reviewAndSave(user);
    await screen.findByRole("button", { name: "Copy Bearer" });
    expect(fetcher).toHaveBeenLastCalledWith(
      "/admin/api/v1/clients",
      expect.objectContaining({
        method: "PUT",
        headers: expect.objectContaining({ "X-QCWS-CSRF": csrf }),
        body: JSON.stringify({
          revision: "clients-2",
          id,
          token: "second-code",
        }),
      }),
    );

    await user.click(screen.getByRole("button", { name: "Done" }));
    await user.click(
      screen.getByRole("button", { name: "Revoke access for " + id }),
    );
    await user.click(
      screen.getByRole("button", { name: "Revoke authorization code" }),
    );
    await screen.findByText(/No client is authorized/);
    expect(fetcher).toHaveBeenLastCalledWith(
      "/admin/api/v1/clients",
      expect.objectContaining({
        method: "DELETE",
        headers: expect.objectContaining({ "X-QCWS-CSRF": csrf }),
        body: JSON.stringify({ revision: "clients-3", id }),
      }),
    );
    expect(
      fetcher.mock.calls.every(([path]) => path === "/admin/api/v1/clients"),
    ).toBe(true);
  },
);

it("reads a saved code only on request, then clears it when hidden and fetches again", async () => {
  const user = userEvent.setup();
  const { fetcher } = service();
  show();
  const reveal = await screen.findByRole("button", { name: "Show saved code" });
  const input = screen.getByLabelText("Saved Bearer authorization code");
  expect(input).toHaveValue("");
  expect(input).toHaveAttribute("type", "password");
  expect(fetcher).toHaveBeenCalledTimes(1);

  await user.click(reveal);
  await waitFor(() => expect(input).toHaveValue("stored-code-for-local"));
  expect(input).toHaveAttribute("type", "text");
  expect(fetcher).toHaveBeenLastCalledWith(
    "/admin/api/v1/clients/reveal",
    expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({ "X-QCWS-CSRF": csrf }),
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify({ revision: "clients-1", id: "local" }),
    }),
  );
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(0);

  await user.click(screen.getByRole("button", { name: "Hide saved code" }));
  expect(input).toHaveValue("");
  expect(input).toHaveAttribute("type", "password");
  await user.click(screen.getByRole("button", { name: "Show saved code" }));
  await waitFor(() => expect(input).toHaveValue("stored-code-for-local"));
  expect(
    fetcher.mock.calls.filter(([path]) => path.endsWith("/reveal")),
  ).toHaveLength(2);
});

it("copies a saved code on demand without displaying it in plain text", async () => {
  const user = userEvent.setup();
  const clipboard = vi
    .spyOn(navigator.clipboard, "writeText")
    .mockResolvedValue();
  const { fetcher } = service();
  show();
  await user.click(
    await screen.findByRole("button", { name: "Copy saved Bearer" }),
  );
  await waitFor(() =>
    expect(clipboard).toHaveBeenCalledWith("stored-code-for-local"),
  );
  expect(
    screen.getByLabelText("Saved Bearer authorization code"),
  ).toHaveAttribute("type", "password");
  expect(document.body).not.toHaveTextContent("stored-code-for-local");
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(0);
});

it("keeps a saved code hidden when clipboard access fails and offers an explicit show action", async () => {
  const user = userEvent.setup();
  vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(
    new Error("clipboard denied"),
  );
  service();
  show();
  await user.click(
    await screen.findByRole("button", { name: "Copy saved Bearer" }),
  );
  expect(
    await screen.findByText(
      "Could not copy. Choose Show saved code to select and copy it manually.",
    ),
  ).toBeVisible();
  expect(
    screen.getByLabelText("Saved Bearer authorization code"),
  ).toHaveAttribute("type", "password");
  expect(screen.getByRole("button", { name: "Show saved code" })).toBeEnabled();
  expect(document.body).not.toHaveTextContent("stored-code-for-local");
});

it("keeps a newly saved code hidden after clipboard failure until the user chooses to show it", async () => {
  const user = userEvent.setup();
  vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(
    new Error("clipboard denied"),
  );
  service();
  show();
  await replaceManually(user, "fresh-copy-failure-code");
  await reviewAndSave(user);
  await user.click(await screen.findByRole("button", { name: "Copy Bearer" }));
  expect(await screen.findByText(/Could not copy\./)).toBeVisible();
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Show code" })).toBeEnabled(),
  );
  expect(screen.getByLabelText("Bearer authorization code")).toHaveAttribute(
    "type",
    "password",
  );
  expect(document.body).not.toHaveTextContent("fresh-copy-failure-code");
});

it.each(["Show saved code", "Copy saved Bearer"])(
  "describes a CLI-pending code as not active yet when using %s",
  async (action) => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    service({ ...initial, pending_restart: true }, { active: false });
    show();
    await user.click(await screen.findByRole("button", { name: action }));
    expect(
      await screen.findByText(
        /saved code is not active yet\. Restart the service to apply it\./i,
      ),
    ).toBeVisible();
    expect(screen.queryByText("Saved and active")).toBeNull();
    if (action === "Show saved code")
      expect(
        screen.getByLabelText("Saved Bearer authorization code"),
      ).toHaveValue("stored-code-for-local");
  },
);

it("keeps legacy digest-only credentials unavailable for reveal while allowing replacement", async () => {
  const user = userEvent.setup();
  const { fetcher } = service({
    ...initial,
    clients: [{ id: "local", source: "manual", can_reveal: false }],
  });
  show();
  const replace = await screen.findByRole("button", {
    name: "Replace code for local",
  });
  expect(screen.queryByRole("button", { name: "Show saved code" })).toBeNull();
  expect(
    screen.queryByRole("button", { name: "Copy saved Bearer" }),
  ).toBeNull();
  await user.click(replace);
  expect(screen.getByLabelText("Bearer authorization code")).toHaveValue("");
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it.each([
  "Refresh clients",
  "Replace code for local",
  "Revoke access for local",
  "Select client second",
])("clears a revealed saved code before %s", async (action) => {
  const user = userEvent.setup();
  service({
    ...initial,
    clients: [
      ...initial.clients,
      { id: "second", source: "manual", can_reveal: true },
    ],
  });
  show();
  await user.click(
    await screen.findByRole("button", { name: "Show saved code" }),
  );
  await waitFor(() =>
    expect(
      screen.getByLabelText("Saved Bearer authorization code"),
    ).toHaveValue("stored-code-for-local"),
  );
  await user.click(screen.getByRole("button", { name: action }));
  await waitFor(() =>
    expect(screen.queryByDisplayValue("stored-code-for-local")).toBeNull(),
  );
});

it.each([
  "Refresh clients",
  "Replace code for local",
  "Revoke access for local",
  "Select client second",
])(
  "ignores a late reveal after %s without locking navigation",
  async (action) => {
    const user = userEvent.setup();
    let finish!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => {
      finish = resolve;
    });
    const { deferNext, fetcher } = service({
      ...initial,
      clients: [
        ...initial.clients,
        { id: "second", source: "manual", can_reveal: true },
      ],
    });
    const { onBusyChange } = show();
    await screen.findByRole("button", { name: "Show saved code" });
    deferNext(pending);
    await user.click(screen.getByRole("button", { name: "Show saved code" }));
    const request = fetcher.mock.calls.find(([path]) =>
      path.endsWith("/reveal"),
    )![1];
    const actionButton = screen.getByRole("button", { name: action });
    expect(actionButton).toBeEnabled();
    expect(onBusyChange).not.toHaveBeenCalledWith(true);
    await user.click(actionButton);
    expect(request.signal?.aborted).toBe(true);
    await act(async () => {
      finish(
        json({
          id: "local",
          token: "late-secret",
          revision: "clients-1",
          active: true,
        }),
      );
      await pending;
    });
    expect(screen.queryByDisplayValue("late-secret")).toBeNull();
    expect(document.body).not.toHaveTextContent("late-secret");
  },
);

it("does not copy a late response after the selected client changes", async () => {
  const user = userEvent.setup();
  const clipboard = vi
    .spyOn(navigator.clipboard, "writeText")
    .mockResolvedValue();
  let finish!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => {
    finish = resolve;
  });
  const { deferNext } = service({
    ...initial,
    clients: [
      ...initial.clients,
      { id: "second", source: "manual", can_reveal: true },
    ],
  });
  show();
  await screen.findByRole("button", { name: "Copy saved Bearer" });
  deferNext(pending);
  await user.click(screen.getByRole("button", { name: "Copy saved Bearer" }));
  await user.click(
    screen.getByRole("button", { name: "Select client second" }),
  );
  await act(async () => {
    finish(
      json({
        id: "local",
        token: "never-copy-late-secret",
        revision: "clients-1",
        active: true,
      }),
    );
    await pending;
  });
  expect(clipboard).not.toHaveBeenCalled();
  expect(screen.queryByDisplayValue("never-copy-late-secret")).toBeNull();
});

it("preserves structured duplicate-credential errors without displaying the server's raw message", async () => {
  const user = userEvent.setup();
  const { deferNext } = service();
  show();
  await replaceManually(user, "already-assigned-code");
  deferNext(
    Promise.resolve(
      json(
        {
          error: {
            code: "invalid_argument",
            reason: "client_credential_duplicate",
            parameters: {
              client_id: "team-qgis",
              token: "server-secret-value",
            },
            message: "raw-secret-from-server",
          },
        },
        400,
      ),
    ),
  );
  await reviewAndSave(user);
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Could not save the authorization code.");
  expect(alert).toHaveTextContent(
    "This code is already assigned to client “team-qgis”. Choose a different code.",
  );
  expect(document.body).not.toHaveTextContent("raw-secret-from-server");
  expect(document.body).not.toHaveTextContent("server-secret-value");
  expect(screen.queryByText("Saved and active")).toBeNull();
});

it("requires an explicit reload after a reveal revision conflict and retries using the current revision", async () => {
  const user = userEvent.setup();
  const { failNext, fetcher } = service();
  show();
  await screen.findByRole("button", { name: "Show saved code" });
  failNext("revision_conflict");
  await user.click(screen.getByRole("button", { name: "Show saved code" }));
  await screen.findByRole("alert");
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(screen.getByLabelText("Saved Bearer authorization code")).toHaveValue(
    "",
  );
  expect(
    screen.getByRole("button", { name: "Show saved code" }),
  ).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Reload clients" }));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Show saved code" }),
    ).toBeEnabled(),
  );
  await user.click(screen.getByRole("button", { name: "Show saved code" }));
  await waitFor(() =>
    expect(
      screen.getByLabelText("Saved Bearer authorization code"),
    ).toHaveValue("stored-code-for-local"),
  );
  expect(fetcher).toHaveBeenLastCalledWith(
    "/admin/api/v1/clients/reveal",
    expect.objectContaining({
      body: JSON.stringify({ revision: "clients-other-session", id: "local" }),
    }),
  );
});
