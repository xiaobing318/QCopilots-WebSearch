import Ajv from "ajv";
import { describe, expect, it, vi } from "vitest";
import viewSchema from "../../../contracts/admin/v1/settings-view.schema.json";
import updateSchema from "../../../contracts/admin/v1/settings-update.schema.json";
import errors from "../../../contracts/admin/v1/error-codes.json";
import reasonSchema from "../../../contracts/admin/v1/error-reasons.json";
import { applyLanguage, getLanguage, translate } from "../i18n/i18n";
import en from "../locales/en.json";
import zhCN from "../locales/zh-CN.json";
import fixture from "./settings.fixture.json";

describe("settings contracts", () => {
  const ajv = new Ajv({ strict: false });
  const view = ajv.compile(viewSchema);
  const update = ajv.compile(updateSchema);

  it("validates the service view and revision-based patches", () => {
    expect(view(fixture)).toBe(true);
    expect(
      update({
        revision: fixture.revision,
        patch: { tools: { open: { enabled: false } } },
      }),
    ).toBe(true);
    expect(update({ patch: {} })).toBe(false);
    expect(
      view({
        ...fixture,
        providers: {
          ...fixture.providers,
          brave: { key_set: true, key: "private" },
        },
      }),
    ).toBe(false);
  });

  it.each(["perplexity", "deepseek"])(
    "accepts selecting %s and exposes only its key state",
    (provider) => {
      const search = { ...fixture.search, provider };
      expect(view({ ...fixture, search })).toBe(true);
      expect(
        update({
          revision: fixture.revision,
          patch: { search: { provider } },
        }),
      ).toBe(true);
      expect(
        view({
          ...fixture,
          search,
          providers: {
            ...fixture.providers,
            [provider]: { key_set: true, key: "private" },
          },
        }),
      ).toBe(false);
    },
  );
});

describe("settings localization", () => {
  it("localizes every registered service error reason in both languages", () => {
    for (const reason of reasonSchema.enum) {
      const key = "errors.reason." + reason;
      expect(Object.hasOwn(en, key), "Missing English reason: " + reason).toBe(
        true,
      );
      expect(
        Object.hasOwn(zhCN, key),
        "Missing Chinese reason: " + reason,
      ).toBe(true);
      expect(translate("en", key)).not.toBe(key);
      expect(translate("zh-CN", key)).not.toBe(key);
    }
  });
  it("keeps both language catalogs in sync with the management error contract", () => {
    expect(Object.keys(en).sort()).toEqual(Object.keys(zhCN).sort());
    for (const code of Object.keys(errors)) {
      expect(translate("en", "errors." + code)).not.toBe("errors." + code);
      expect(translate("zh-CN", "errors." + code)).not.toBe("errors." + code);
    }
  });

  it("uses browser preferences until a language is saved", () => {
    vi.spyOn(navigator, "languages", "get").mockReturnValue([
      "fr-FR",
      "zh-Hant",
      "en",
    ]);
    expect(getLanguage()).toBe("zh-CN");
    applyLanguage("en");
    expect(document.documentElement.lang).toBe("en");
    expect(localStorage.getItem("qcws.language")).toBe("en");
    expect(getLanguage()).toBe("en");
  });
});
