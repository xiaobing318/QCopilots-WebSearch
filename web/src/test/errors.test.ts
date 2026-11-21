import { describe, expect, it, vi } from "vitest";
import { admin, ApiError } from "../admin";
import { errorLabel, errorMessage, localized, renderMessage } from "../labels";

function response(body: unknown, status = 400, headers: HeadersInit = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

async function requestError(value: Response | Error): Promise<ApiError> {
  vi.stubGlobal(
    "fetch",
    value instanceof Response
      ? vi.fn().mockResolvedValue(value)
      : vi.fn().mockRejectedValue(value),
  );
  try {
    await admin.clients();
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError);
    return error as ApiError;
  }
  throw new Error("The request unexpectedly succeeded");
}

describe("management request errors", () => {
  it("retains only safe structured details and localizes reason, context and retry", async () => {
    const error = await requestError(
      response({
        error: {
          code: "invalid_argument",
          reason: "client_credential_duplicate",
          parameters: {
            client_id: "team-qgis",
            token: "must-not-appear",
            path: "private-path",
          },
          message: "raw server secret must-not-appear",
          retry_after_seconds: 12,
        },
      }),
    );
    expect(error).toMatchObject({
      code: "invalid_argument",
      reason: "client_credential_duplicate",
      parameters: { client_id: "team-qgis" },
      status: 400,
      retry_after_seconds: 12,
    });
    expect(Object.keys(error.parameters)).toEqual(["client_id"]);
    expect(error.message).toBe("invalid_argument");
    const message = errorMessage(error, "clientSave");
    const english = renderMessage(localized("en"), message);
    const chinese = renderMessage(localized("zh-CN"), message);
    expect(english).toContain("Could not save the authorization code.");
    expect(english).toContain("client “team-qgis”");
    expect(english).toContain("12 seconds");
    expect(chinese).toContain("无法保存授权码");
    expect(chinese).toContain("客户端“team-qgis”");
    expect(chinese).toContain("12 秒");
    expect(english + chinese + JSON.stringify(error)).not.toMatch(
      /must-not-appear|private-path|raw server secret/,
    );
  });

  it("rejects unsafe client parameters without leaving an unresolved placeholder", async () => {
    const error = await requestError(
      response({
        error: {
          code: "invalid_argument",
          reason: "client_credential_duplicate",
          parameters: {
            client_id: '<img src=x onerror="secret">',
            endpoint: "private-endpoint",
          },
        },
      }),
    );
    const message = renderMessage(localized("en"), errorMessage(error));
    expect(error.parameters).toEqual({});
    expect(message).toContain("another client");
    expect(message).not.toMatch(/img|secret|private-endpoint|client_id/);
  });

  it.each([
    "",
    "<html>private-proxy-conflict</html>",
    JSON.stringify({ error: { message: "private-conflict-details" } }),
    JSON.stringify({
      error: { code: "invalid/code", message: "private-conflict-details" },
    }),
  ])(
    "keeps an unclassified HTTP 409 unknown instead of guessing a revision conflict %#",
    async (body) => {
      const error = await requestError(new Response(body, { status: 409 }));
      expect(error.code).toBe("unknown");
      expect(error.status).toBe(409);
      expect(error.reason).toBeUndefined();
      expect(errorLabel(error)).toBe("errors.unknown");
      const text = renderMessage(localized("en"), errorMessage(error));
      expect(text).toContain("Error reference: unknown (HTTP 409).");
      expect(text).not.toMatch(
        /revision|another session|private-proxy-conflict|private-conflict-details/,
      );
    },
  );

  it("recognizes a revision conflict only when the error explicitly identifies it", async () => {
    const error = await requestError(
      response({ error: { code: "revision_conflict" } }, 409),
    );
    expect(error).toMatchObject({ code: "revision_conflict", status: 409 });
    expect(errorLabel(error)).toBe("errors.revision_conflict");
  });

  it("classifies a non-JSON successful response separately from a failed connection", async () => {
    const malformed = await requestError(
      new Response("<html>private-response</html>", { status: 200 }),
    );
    const network = await requestError(
      new TypeError("private-network-endpoint"),
    );
    expect(malformed).toMatchObject({ code: "invalid_response", status: 200 });
    expect(network.code).toBe("network_error");
    expect(network.status).toBeUndefined();
    const text =
      renderMessage(localized("en"), errorMessage(malformed)) +
      renderMessage(localized("en"), errorMessage(network));
    expect(text).not.toMatch(
      /private-response|private-network-endpoint|stopped|not running/,
    );
  });

  it.each([400, 500, 502])(
    "classifies an HTML HTTP %s error without guessing its cause",
    async (status) => {
      const error = await requestError(
        new Response("<html>private-proxy-diagnostics</html>", { status }),
      );
      expect(error).toMatchObject({ code: "invalid_response", status });
      const text = renderMessage(localized("en"), errorMessage(error));
      expect(text).not.toMatch(
        /private-proxy-diagnostics|input|stopped|not running/,
      );
    },
  );

  it("classifies cancellation rather than reporting a network failure", async () => {
    const error = await requestError(
      new DOMException("private cancellation detail", "AbortError"),
    );
    expect(error.code).toBe("request_cancelled");
    expect(renderMessage(localized("en"), errorMessage(error))).toBe(
      "The request was cancelled.",
    );
  });

  it("keeps unknown server reasons out of the UI and falls back to the known code", async () => {
    const error = await requestError(
      response({
        error: {
          code: "invalid_configuration",
          reason: "future_private_reason",
          message: "service stopped at private-path",
        },
      }),
    );
    const message = errorMessage(error, "clientReveal");
    expect(message.key).toBe("errors.invalid_configuration");
    expect(renderMessage(localized("en"), message)).not.toMatch(
      /future_private_reason|stopped|private-path/,
    );
    expect(errorMessage(new ApiError("future_code")).key).toBe(
      "errors.unknown",
    );
  });

  it("renders a safe unknown code, HTTP status and retry delay in both languages", async () => {
    const error = await requestError(
      response(
        {
          error: {
            code: "future_failure",
            reason: "future_reason",
            message: "private-server-detail",
            retry_after_seconds: 12,
          },
        },
        418,
      ),
    );
    const message = errorMessage(error, "clientSave");
    expect(message).toMatchObject({
      key: "errors.unknown",
      code: "future_failure",
      status: 418,
      retryAfter: 12,
    });
    const english = renderMessage(localized("en"), message);
    const chinese = renderMessage(localized("zh-CN"), message);
    expect(english).toContain("Could not save the authorization code.");
    expect(english).toContain("Error reference: future_failure (HTTP 418).");
    expect(english).toContain("Try again in 12 seconds.");
    expect(chinese).toContain("无法保存授权码。");
    expect(chinese).toContain("错误标识：future_failure，HTTP 418。");
    expect(chinese).toContain("请在 12 秒后重试。");
    expect(english + chinese).not.toMatch(
      /future_reason|private-server-detail/,
    );
  });

  it("omits HTTP from an unknown error reference when no status was received", () => {
    const message = errorMessage(new ApiError("future_failure"));
    expect(message).toMatchObject({
      key: "errors.unknown",
      code: "future_failure",
    });
    expect(message.status).toBeUndefined();
    const english = renderMessage(localized("en"), message);
    const chinese = renderMessage(localized("zh-CN"), message);
    expect(english).toContain("Error reference: future_failure.");
    expect(chinese).toContain("错误标识：future_failure。");
    expect(english + chinese).not.toContain("HTTP");
  });

  it("does not treat a diagnostic translation name as a known server error code", () => {
    const message = errorMessage(new ApiError("reference", { status: 500 }));
    expect(message.key).toBe("errors.unknown");
    expect(renderMessage(localized("en"), message)).toContain(
      "Error reference: reference (HTTP 500).",
    );
    expect(renderMessage(localized("zh-CN"), message)).toContain(
      "错误标识：reference，HTTP 500。",
    );
  });

  it.each(["private/path", "PRIVATE", "", "x".repeat(65), "private\nheader"])(
    "does not expose an invalid diagnostic code %#",
    (code) => {
      const english = renderMessage(localized("en"), {
        key: "errors.unknown",
        code,
        status: 999,
      });
      const chinese = renderMessage(localized("zh-CN"), {
        key: "errors.unknown",
        code,
        status: 999,
      });
      expect(english).toContain("Error reference: unknown.");
      expect(chinese).toContain("错误标识：unknown。");
      expect(english + chinese).not.toMatch(/HTTP|999|private|PRIVATE|x{65}/);
    },
  );

  it.each([99, 600, -1, 400.5, Number.NaN, Number.POSITIVE_INFINITY])(
    "does not expose an invalid diagnostic HTTP status %s",
    (status) => {
      const message = { key: "errors.unknown", code: "future_failure", status };
      expect(renderMessage(localized("en"), message)).toContain(
        "Error reference: future_failure.",
      );
      expect(renderMessage(localized("zh-CN"), message)).not.toContain("HTTP");
    },
  );

  it.each([100, 599])(
    "accepts diagnostic HTTP status boundary %s",
    (status) => {
      const message = { key: "errors.unknown", code: "future_failure", status };
      expect(renderMessage(localized("en"), message)).toContain(
        `Error reference: future_failure (HTTP ${status}).`,
      );
      expect(renderMessage(localized("zh-CN"), message)).toContain(
        `错误标识：future_failure，HTTP ${status}。`,
      );
    },
  );

  it("does not append diagnostic references to known errors or known reasons", () => {
    for (const error of [
      new ApiError("invalid_argument", { status: 400, retry_after_seconds: 3 }),
      new ApiError("future_failure", {
        reason: "provider_rate_limited",
        status: 429,
        retry_after_seconds: 3,
      }),
    ]) {
      const english = renderMessage(localized("en"), errorMessage(error));
      const chinese = renderMessage(localized("zh-CN"), errorMessage(error));
      expect(english + chinese).not.toMatch(
        /Error reference|错误标识|HTTP|future_failure/,
      );
      expect(english).toContain("Try again in 3 seconds.");
      expect(chinese).toContain("请在 3 秒后重试。");
    }
  });

  it("uses a numeric Retry-After header when the body has no delay", async () => {
    const error = await requestError(
      new Response("", { status: 429, headers: { "Retry-After": "30" } }),
    );
    expect(error).toMatchObject({
      code: "rate_limited",
      status: 429,
      retry_after_seconds: 30,
    });
    expect(renderMessage(localized("en"), errorMessage(error))).toContain(
      "30 seconds",
    );
  });

  it("uses the validated structured retry delay before a conflicting header", async () => {
    const error = await requestError(
      response(
        {
          error: {
            code: "rate_limited",
            reason: "provider_rate_limited",
            retry_after_seconds: 5,
          },
        },
        429,
        { "Retry-After": "30" },
      ),
    );
    expect(error.retry_after_seconds).toBe(5);
    expect(errorMessage(error, "providerTest").key).toBe(
      "errors.reason.provider_rate_limited",
    );
    expect(
      renderMessage(localized("zh-CN"), errorMessage(error, "providerTest")),
    ).toContain("搜索来源正在限制请求");
  });

  it.each([-1, 1.5, 2147483648, "20", Number.NaN])(
    "does not display an invalid retry delay %s",
    (delay) => {
      const error = ApiError.fromDetails({
        code: "rate_limited",
        retry_after_seconds: delay,
      });
      expect(error.retry_after_seconds).toBeUndefined();
      expect(renderMessage(localized("en"), errorMessage(error))).not.toContain(
        "seconds",
      );
    },
  );

  it("localizes embedded provider-test details without using the provider's message", () => {
    const error = ApiError.fromDetails({
      code: "provider_unavailable",
      message: "upstream-url-private",
      retry_after_seconds: 5,
    });
    const text = renderMessage(
      localized("en"),
      errorMessage(error, "providerTest"),
    );
    expect(text).toContain("provider test");
    expect(text).toContain("5 seconds");
    expect(text).not.toContain("upstream-url-private");
  });
});

describe("saved authorization response validation", () => {
  const valid = {
    id: "..",
    revision: "revision-1",
    token: "xbyang/qcopilots",
    active: false,
  };

  it("posts the client ID and revision in a CSRF-protected no-store request", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(response({ ...valid, unexpected: "ignored" }, 200));
    vi.stubGlobal("fetch", fetcher);
    const controller = new AbortController();
    await expect(
      admin.revealClient(
        valid.revision,
        valid.id,
        "csrf-value",
        controller.signal,
      ),
    ).resolves.toEqual(valid);
    expect(fetcher).toHaveBeenCalledWith(
      "/admin/api/v1/clients/reveal",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
        signal: controller.signal,
        headers: expect.objectContaining({ "X-QCWS-CSRF": "csrf-value" }),
        body: JSON.stringify({ revision: valid.revision, id: valid.id }),
      }),
    );
  });

  it.each([
    { id: "other-client" },
    { revision: "changed-revision" },
    { token: "" },
    { token: "not allowed" },
    { token: "line\nbreak" },
    { token: "码" },
    { token: "x".repeat(8193) },
    { token: 12 },
    { active: "false" },
    { active: undefined },
  ])("rejects an invalid reveal response %#", async (patch) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(response({ ...valid, ...patch }, 200)),
    );
    await expect(
      admin.revealClient(valid.revision, valid.id, "csrf-value"),
    ).rejects.toMatchObject({ code: "invalid_response", status: 200 });
  });

  it("discards a response when its request was cancelled before delivery", async () => {
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => {
        controller.abort();
        return response(valid, 200);
      }),
    );
    await expect(
      admin.revealClient(
        valid.revision,
        valid.id,
        "csrf-value",
        controller.signal,
      ),
    ).rejects.toMatchObject({ code: "request_cancelled" });
  });

  it("retains the actual HTTP status when reveal response validation fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(response({ ...valid, active: "invalid" }, 202)),
    );
    await expect(
      admin.revealClient(valid.revision, valid.id, "csrf-value"),
    ).rejects.toMatchObject({ code: "invalid_response", status: 202 });
  });
});
