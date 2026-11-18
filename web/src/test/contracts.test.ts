import Ajv from "ajv";
import { describe, expect, it, vi } from "vitest";
import viewSchema from "../../../contracts/admin/v1/settings-view.schema.json";
import updateSchema from "../../../contracts/admin/v1/settings-update.schema.json";
import errors from "../../../contracts/admin/v1/error-codes.json";
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
});

describe("settings localization", () => {
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
