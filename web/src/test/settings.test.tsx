import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import {
  readLoginCode,
  type ClientRegistryView,
  type SettingsView,
} from "../admin";
import { App } from "../App";
import fixture from "./settings.fixture.json";

const csrf = "session-csrf";
const defaultStatus = {
  version: "0.2.0",
  mcp_url: "https://web-search.vastarmorai.com/mcp",
  listeners: {
    business: { host: "127.0.0.1", port: 3181 },
    control: { host: "127.0.0.1", port: 3182 },
    admin: { host: "127.0.0.1", port: 3183 },
  },
  cooldown: { duckduckgo: { retry_after_seconds: 60 } },
  settings_error: null,
};

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockService({
  signedIn = true,
  pendingClientSave,
}: {
  signedIn?: boolean;
  pendingClientSave?: Promise<Response>;
} = {}) {
  let authenticated = signedIn;
  let current = structuredClone(fixture) as SettingsView;
  let clients: ClientRegistryView = {
    revision: "clients-v1",
    clients: [{ id: "local", source: "file" }],
    pending_restart: false,
  };
  const fetcher = vi.fn((path: string, options: RequestInit) => {
    const key = options.method + " " + path;
    switch (key) {
      case "GET /admin/api/v1/session":
        return json(
          authenticated ? { csrf } : { error: { code: "unauthorized" } },
          authenticated ? 200 : 401,
        );
      case "POST /admin/api/v1/session":
        authenticated = true;
        return json({ csrf });
      case "DELETE /admin/api/v1/session":
        authenticated = false;
        return json({ ok: true });
      case "GET /admin/api/v1/settings":
        return json(current);
      case "GET /admin/api/v1/clients":
        return json(clients);
      case "PUT /admin/api/v1/clients": {
        const { id } = JSON.parse(String(options.body));
        if (!clients.clients.some((client) => client.id === id))
          return json({ error: { code: "not_found" } }, 404);
        if (pendingClientSave) return pendingClientSave;
        clients = {
          ...clients,
          revision: "clients-v2",
          clients: clients.clients.map((client) =>
            client.id === id ? { id, source: "manual" } : client,
          ),
        };
        return json(clients);
      }
      case "PUT /admin/api/v1/settings": {
        const { patch } = JSON.parse(String(options.body));
        current = {
          ...current,
          ...patch,
          tools: { ...current.tools, ...patch.tools },
          search: { ...current.search, ...patch.search },
          revision: "fedcba0987654321",
        };
        return json(current);
      }
      case "GET /admin/api/v1/status":
        return json(defaultStatus);
      case "POST /admin/api/v1/providers/auto/test":
        return json({
          ok: true,
          source: "bing",
          result_count: 2,
          elapsed_ms: 15,
          sample: [
            { title: "QGIS", url: "https://qgis.org/" },
            { title: "Invalid URL", url: "javascript:void(0)" },
          ],
        });
      default: {
        const selected = current.search.provider;
        const keyPath = "/admin/api/v1/providers/" + selected + "/key";
        if (
          selected !== "auto" &&
          path === keyPath &&
          (options.method === "PUT" || options.method === "DELETE")
        ) {
          current.providers[selected].key_set = options.method === "PUT";
          return json(current);
        }
        return json({ error: { code: "not_found" } }, 404);
      }
    }
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

it("clears and exchanges the one-time login code", async () => {
  const fetcher = mockService({ signedIn: false });
  history.replaceState(null, "", "/settings/#login=generated-sign-in-code");
  const loginCode = readLoginCode();
  expect(window.location.hash).toBe("");
  render(<App loginCode={loginCode} />);
  expect(
    await screen.findByRole("heading", { name: "Access authorization" }),
  ).toBeVisible();
  expect(fetcher).toHaveBeenCalledWith(
    "/admin/api/v1/session",
    expect.objectContaining({
      method: "POST",
      body: '{"code":"generated-sign-in-code"}',
      credentials: "same-origin",
      redirect: "error",
    }),
  );
  expect(document.body).not.toHaveTextContent("generated-sign-in-code");
  expect(localStorage.getItem("generated-sign-in-code")).toBeNull();
});

it("accepts an administrator token and sends CSRF when signing out", async () => {
  const user = userEvent.setup();
  const fetcher = mockService({ signedIn: false });
  render(<App />);
  await user.type(
    await screen.findByLabelText("Administrator token"),
    "sensitive-admin-token",
  );
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  expect(
    await screen.findByRole("heading", { name: "Access authorization" }),
  ).toBeVisible();
  expect(document.body).not.toHaveTextContent("sensitive-admin-token");
  await user.click(screen.getByRole("button", { name: "Sign out" }));
  expect(
    await screen.findByRole("heading", { name: "Sign in to Web Search" }),
  ).toBeVisible();
  expect(fetcher).toHaveBeenCalledWith(
    "/admin/api/v1/session",
    expect.objectContaining({
      method: "DELETE",
      headers: expect.objectContaining({ "X-QCWS-CSRF": csrf }),
    }),
  );
});

it("includes the current revision and only the modified preference in a write", async () => {
  const user = userEvent.setup();
  const fetcher = mockService();
  render(<App />);
  await user.click(await screen.findByRole("tab", { name: "Search settings" }));
  const tool = await screen.findByRole("switch", { name: "Open web pages" });
  expect(tool).toBeChecked();
  await user.click(tool);
  await waitFor(() => expect(tool).not.toBeChecked());
  expect(fetcher).toHaveBeenCalledWith(
    "/admin/api/v1/settings",
    expect.objectContaining({
      method: "PUT",
      headers: expect.objectContaining({ "X-QCWS-CSRF": csrf }),
      body: '{"revision":"1234567890abcdef","patch":{"tools":{"open":{"enabled":false}}}}',
    }),
  );
  expect(
    screen.queryByRole("switch", { name: "Search without asking" }),
  ).toBeNull();
});

it.each([
  ["brave", "Brave Search", "https://brave.com/search/api/"],
  ["perplexity", "Perplexity", "https://console.perplexity.ai/project/keys"],
  ["deepseek", "DeepSeek", "https://platform.deepseek.com/api_keys"],
] as const)(
  "shows only key state after saving or removing a %s key",
  async (provider, label, keyUrl) => {
    const user = userEvent.setup();
    const fetcher = mockService();
    render(<App />);
    await user.click(
      await screen.findByRole("tab", { name: "Search settings" }),
    );
    const source = await screen.findByRole("combobox", { name: "Provider" });
    await user.selectOptions(source, label);
    const key = await screen.findByLabelText("Provider key");
    expect(key).toHaveAttribute("type", "password");
    expect(screen.getByRole("link", { name: "Get a key" })).toHaveAttribute(
      "href",
      keyUrl,
    );
    await user.type(key, "example-secret-key");
    await user.click(screen.getByRole("button", { name: "Save key" }));
    expect(await screen.findByText("Key configured")).toBeVisible();
    expect(key).toHaveValue("");
    expect(document.body).not.toHaveTextContent("example-secret-key");
    const keyPath = "/admin/api/v1/providers/" + provider + "/key";
    expect(fetcher).toHaveBeenCalledWith(
      keyPath,
      expect.objectContaining({
        method: "PUT",
        body: '{"key":"example-secret-key"}',
      }),
    );
    await user.click(screen.getByRole("button", { name: "Remove key" }));
    expect(await screen.findByText("No key configured")).toBeVisible();
    expect(fetcher).toHaveBeenCalledWith(
      keyPath,
      expect.objectContaining({ method: "DELETE" }),
    );
  },
);

it("persists the selected language and displays provider status and safe samples", async () => {
  const user = userEvent.setup();
  mockService();
  render(<App />);
  await user.click(await screen.findByRole("tab", { name: "Service status" }));
  expect(await screen.findByText("DuckDuckGo · 60 s remaining")).toBeVisible();
  await user.click(screen.getByRole("tab", { name: "Search settings" }));
  await user.click(screen.getByRole("button", { name: "Test provider" }));
  expect(
    await screen.findByText("Bing returned 2 results in 15 ms."),
  ).toBeVisible();
  expect(screen.getByRole("link", { name: "QGIS" })).toHaveAttribute(
    "href",
    "https://qgis.org/",
  );
  expect(screen.queryByRole("link", { name: "Invalid URL" })).toBeNull();
  await user.selectOptions(
    screen.getByRole("combobox", { name: "Language" }),
    "zh-CN",
  );
  expect(document.documentElement.lang).toBe("zh-CN");
  expect(localStorage.getItem("qcws.language")).toBe("zh-CN");
  expect(
    screen.queryByRole("heading", { name: "Access authorization" }),
  ).toBeNull();
});

it("supports changing sources and saving a SearXNG endpoint", async () => {
  const user = userEvent.setup();
  const fetcher = mockService();
  render(<App />);
  await user.click(await screen.findByRole("tab", { name: "Search settings" }));
  const provider = await screen.findByRole("combobox", { name: "Provider" });
  await user.selectOptions(provider, "searxng");
  const endpoint = await screen.findByLabelText("SearXNG endpoint");
  await user.clear(endpoint);
  await user.type(endpoint, "https://search.example.org/search");
  await user.click(
    screen.getByRole("button", { name: "Save provider settings" }),
  );
  await waitFor(() => expect(provider).toBeEnabled());
  expect(fetcher).toHaveBeenCalledWith(
    "/admin/api/v1/settings",
    expect.objectContaining({
      method: "PUT",
      body: '{"revision":"fedcba0987654321","patch":{"search":{"searxng":{"url":"https://search.example.org/search","engines":["google","bing","duckduckgo"]}}}}',
    }),
  );
  await user.selectOptions(provider, "brave");
  expect(await screen.findByRole("link", { name: "Get a key" })).toBeVisible();
  await user.selectOptions(provider, "auto");
  await waitFor(() =>
    expect(screen.queryByLabelText("Provider key")).toBeNull(),
  );
});

it("navigates the three tabs with arrow keys, Home and End", async () => {
  const user = userEvent.setup();
  mockService();
  render(<App />);
  const access = await screen.findByRole("tab", {
    name: "Access authorization",
  });
  const search = screen.getByRole("tab", { name: "Search settings" });
  const status = screen.getByRole("tab", { name: "Service status" });
  expect(access).toHaveAttribute("aria-selected", "true");
  expect(search).toHaveAttribute("tabindex", "-1");
  access.focus();
  await user.keyboard("{ArrowRight}");
  expect(search).toHaveFocus();
  expect(search).toHaveAttribute("aria-selected", "true");
  expect(
    screen.getByRole("tabpanel", { name: "Search settings" }),
  ).toBeVisible();
  await user.keyboard("{End}");
  expect(status).toHaveFocus();
  expect(status).toHaveAttribute("aria-selected", "true");
  await user.keyboard("{ArrowRight}");
  expect(access).toHaveFocus();
  await user.keyboard("{ArrowLeft}");
  expect(status).toHaveFocus();
  await user.keyboard("{Home}");
  expect(access).toHaveFocus();
  expect(access).toHaveAttribute("aria-selected", "true");
});

it("forgets unsaved text when switching tabs and keeps saved codes unavailable after sign-out", async () => {
  const user = userEvent.setup();
  const clipboard = vi
    .spyOn(navigator.clipboard, "writeText")
    .mockResolvedValue();
  mockService();
  render(<App />);
  await user.click(
    await screen.findByRole("button", { name: "Replace code for local" }),
  );
  await user.click(screen.getByRole("button", { name: "Generate from text" }));
  await user.type(
    screen.getByLabelText("Text to generate from"),
    "temporary-source",
  );
  await user.click(screen.getByRole("tab", { name: "Search settings" }));
  await user.click(screen.getByRole("tab", { name: "Access authorization" }));
  await user.click(
    await screen.findByRole("button", { name: "Replace code for local" }),
  );
  await user.click(screen.getByRole("button", { name: "Generate from text" }));
  expect(screen.getByLabelText("Text to generate from")).toHaveValue("");
  await user.click(screen.getByRole("button", { name: "Enter manually" }));
  await user.type(
    screen.getByLabelText("Bearer authorization code"),
    "saved-only-in-memory",
  );
  await user.click(screen.getByRole("button", { name: "Next: review save" }));
  await user.click(screen.getByRole("button", { name: "Save and activate" }));
  await user.click(
    await screen.findByRole("button", { name: "Copy server URL" }),
  );
  expect(clipboard).toHaveBeenLastCalledWith(defaultStatus.mcp_url);
  await user.click(screen.getByRole("button", { name: "Sign out" }));
  const adminToken = await screen.findByLabelText("Administrator token");
  expect(screen.queryByDisplayValue("saved-only-in-memory")).toBeNull();
  await user.type(adminToken, "admin-token");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  await user.click(
    await screen.findByRole("button", { name: "Replace code for local" }),
  );
  expect(screen.getByLabelText("Bearer authorization code")).toHaveValue("");
  expect(screen.queryByRole("button", { name: "Copy Bearer" })).toBeNull();
});

it("forgets the saved temporary Bearer when leaving and returning to the authorization tab", async () => {
  const user = userEvent.setup();
  mockService();
  render(<App />);
  await user.click(
    await screen.findByRole("button", { name: "Replace code for local" }),
  );
  await user.click(screen.getByRole("button", { name: "Enter manually" }));
  await user.type(
    screen.getByLabelText("Bearer authorization code"),
    "tab-sensitive-code",
  );
  await user.click(screen.getByRole("button", { name: "Next: review save" }));
  await user.click(screen.getByRole("button", { name: "Save and activate" }));
  await screen.findByRole("button", { name: "Copy Bearer" });
  await user.click(screen.getByRole("tab", { name: "Service status" }));
  expect(screen.queryByDisplayValue("tab-sensitive-code")).toBeNull();
  await user.click(screen.getByRole("tab", { name: "Access authorization" }));
  await screen.findByRole("button", { name: "Replace code for local" });
  expect(screen.queryByRole("button", { name: "Copy Bearer" })).toBeNull();
});

it("prevents changing tabs or signing out while an authorization save is pending", async () => {
  const user = userEvent.setup();
  let finish!: (response: Response) => void;
  const pendingClientSave = new Promise<Response>((resolve) => {
    finish = resolve;
  });
  mockService({ pendingClientSave });
  render(<App />);
  await user.click(
    await screen.findByRole("button", { name: "Replace code for local" }),
  );
  await user.click(screen.getByRole("button", { name: "Enter manually" }));
  await user.type(
    screen.getByLabelText("Bearer authorization code"),
    "pending-code",
  );
  await user.click(screen.getByRole("button", { name: "Next: review save" }));
  await user.click(screen.getByRole("button", { name: "Save and activate" }));
  for (const tab of screen.getAllByRole("tab")) expect(tab).toBeDisabled();
  expect(screen.getByRole("button", { name: "Sign out" })).toBeDisabled();
  await act(async () => {
    finish(
      json({
        revision: "clients-v2",
        clients: [{ id: "local", source: "manual" }],
        pending_restart: false,
      }),
    );
    await pendingClientSave;
  });
  expect(
    await screen.findByRole("button", { name: "Copy Bearer" }),
  ).toBeEnabled();
  for (const tab of screen.getAllByRole("tab")) expect(tab).toBeEnabled();
  expect(screen.getByRole("button", { name: "Sign out" })).toBeEnabled();
});
