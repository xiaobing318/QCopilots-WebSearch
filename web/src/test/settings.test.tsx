import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { readLoginCode, type SettingsView } from "../admin";
import { App } from "../App";
import fixture from "./settings.fixture.json";

const csrf = "session-csrf";
const defaultStatus = {
  version: "0.2.0",
  listeners: {
    business: { host: "127.0.0.1", port: 8088 },
    control: { host: "127.0.0.1", port: 8089 },
    admin: { host: "127.0.0.1", port: 8091 },
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
  provider = "auto",
}: {
  signedIn?: boolean;
  provider?: SettingsView["search"]["provider"];
} = {}) {
  let authenticated = signedIn;
  let current = {
    ...structuredClone(fixture),
    search: { ...fixture.search, provider },
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
      case "PUT /admin/api/v1/providers/brave/key":
        current.providers.brave.key_set = true;
        return json(current);
      case "DELETE /admin/api/v1/providers/brave/key":
        current.providers.brave.key_set = false;
        return json(current);
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
      default:
        return json({ error: { code: "not_found" } }, 404);
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
    await screen.findByRole("heading", { name: "Web Search Settings" }),
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
    await screen.findByRole("heading", { name: "Web Search Settings" }),
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
  await user.click(
    screen.getByRole("switch", { name: "Search without asking" }),
  );
  expect(fetcher).toHaveBeenCalledWith(
    "/admin/api/v1/settings",
    expect.objectContaining({
      method: "PUT",
      body: '{"revision":"fedcba0987654321","patch":{"search_without_asking":true}}',
    }),
  );
});

it("shows only key state after saving or removing a provider key", async () => {
  const user = userEvent.setup();
  mockService({ provider: "brave" });
  render(<App />);
  const key = await screen.findByLabelText("Provider key");
  expect(key).toHaveAttribute("type", "password");
  await user.type(key, "example-secret-key");
  await user.click(screen.getByRole("button", { name: "Save key" }));
  expect(await screen.findByText("Key configured")).toBeVisible();
  expect(key).toHaveValue("");
  expect(document.body).not.toHaveTextContent("example-secret-key");
  await user.click(screen.getByRole("button", { name: "Remove key" }));
  expect(await screen.findByText("No key configured")).toBeVisible();
});

it("persists the selected language and displays provider status and safe samples", async () => {
  const user = userEvent.setup();
  mockService();
  render(<App />);
  expect(await screen.findByText("DuckDuckGo · 60 s remaining")).toBeVisible();
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
    screen.queryByRole("heading", { name: "Web Search Settings" }),
  ).toBeNull();
});

it("supports changing sources and saving a SearXNG endpoint", async () => {
  const user = userEvent.setup();
  const fetcher = mockService();
  render(<App />);
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
