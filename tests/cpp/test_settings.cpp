#include "test_support.hpp"
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QTemporaryDir>
#include <iostream>
#include <qws/settings.hpp>

int main(int argc, char **argv) {
  QCoreApplication app(argc, argv);
  try {
    const auto requireReason = [](const char *code, const char *reason, const auto &action) {
      try {
        action();
      } catch (const qws::Error &error) {
        require(error.code == code, "Unexpected diagnostic error code.");
        require(error.reason == reason, "Unexpected diagnostic reason.");
        require(qws::errorPayload(error).dump().find("diagnostic-secret-sentinel") == std::string::npos,
                "An error diagnostic exposed caller data.");
        return;
      }
      throw std::runtime_error("Expected a diagnostic error.");
    };
    QTemporaryDir temporary;
    require(temporary.isValid(), "Temporary directory required.");
    qws::Config config{qws::defaultConfiguration(), temporary.path().toStdString()};
    auto endpoint = config.value.at("business");
    require(qws::businessMcpUrl(endpoint) == "http://127.0.0.1:3181/mcp",
            "An unconfigured public endpoint must describe the local business listener.");
    endpoint["host"] = "::1";
    endpoint["port"] = 0;
    require(qws::businessMcpUrl(endpoint, 53181) == "http://[::1]:53181/mcp",
            "MCP metadata must bracket IPv6 and use the actual bound port.");
    endpoint["tls"] = qws::Json::object();
    require(qws::businessMcpUrl(endpoint, 53181) == "https://[::1]:53181/mcp",
            "Local MCP metadata must reflect business TLS.");
    endpoint["public_url"] = "https://search.example.test:8443/mcp";
    require(qws::businessMcpUrl(endpoint, 53181) == "https://search.example.test:8443/mcp",
            "An explicit deployment URL must take precedence over the local listener.");
    require(qws::adminConfiguration(config, false).port == 3183,
            "New configurations must explicitly select administration port 3183.");
    auto legacy = config;
    legacy.value.erase("admin");
    require(qws::adminConfiguration(legacy, false).port == 8091,
            "Legacy configurations without administration options retain port 8091.");
    legacy.value["admin"] = {{"port", 8123}};
    require(qws::adminConfiguration(legacy, false).port == 8123,
            "An existing explicit administration port must be preserved.");
    qws::SettingsStore fresh(config);
    auto initial = fresh.current();
    require(fresh.provider(*initial).id == "auto", "New installs default to auto.");
    require(!initial->value["search_without_asking"].get<bool>(), "Confirmation defaults to on.");
    config.value["provider"] = {{"url", "http://127.0.0.1:8818/search"}, {"engines", {"bing"}}};
    qws::SettingsStore settings(config);
    const auto old = settings.current();
    require(settings.provider(*old).id == "searxng", "Legacy configuration must select SearXNG.");
    require(settings.provider(*old).engines == std::vector<std::string>{"bing"}, "Legacy engines must survive.");
    auto view = settings.update({{"search", {{"provider", "brave"}}}}, old->revision);
    require(view["revision"] != old->revision, "Changing settings must change the revision.");
    require(settings.provider(*old).id == "searxng", "Snapshots must remain immutable.");
    requireError("revision_conflict", [&] { settings.update(qws::Json::object(), old->revision); });
    requireError("invalid_argument", [&] { settings.update({{"unexpected", true}}, view["revision"]); });
    view = settings.setKey("brave", "example-provider-key");
    require(view["providers"]["brave"]["key_set"] == true, "Key state must be reported.");
    require(view.dump().find("example-provider-key") == std::string::npos, "Settings must not expose keys.");
    qws::SettingsStore reopened(config);
    require(reopened.provider(*reopened.current()).key == "example-provider-key", "The key must persist.");
    require(reopened.clearKey("brave")["providers"]["brave"]["key_set"] == false, "Keys can be cleared.");
    QFile invalid(QDir(temporary.path()).filePath("state/settings.json"));
    require(invalid.open(QIODevice::WriteOnly) && invalid.write("{") == 1, "Cannot write fixture.");
    invalid.close();
    reopened.reloadIfChanged();
    require(reopened.error() == "settings_error", "Invalid external edits must be reported.");
    require(reopened.errorDetails()["reason"] == "settings_json_invalid",
            "Invalid JSON must have a precise storage diagnostic.");
    require(reopened.provider(*reopened.current()).id == "brave", "Invalid edits must retain last settings.");

    for (const auto *id : {"perplexity", "deepseek"}) {
      const auto key = std::string("example-") + id + "-key";
      view = reopened.update({{"search", {{"provider", id}}}}, reopened.current()->revision);
      require(view["search"]["provider"] == id, "New providers can be selected.");
      require(view["providers"][id]["key_set"] == false, "New providers start without a key.");
      require(reopened.policy(*reopened.current())["provider_ready"] == false, "A key is required.");
      view = reopened.setKey(id, key);
      qws::validateSchema(qws::readSchema("settings-view"), view);
      require(view["providers"][id]["key_set"] == true, "New provider key state must be reported.");
      require(view.dump().find(key) == std::string::npos, "Settings must not expose new provider keys.");
      qws::SettingsStore persisted(config);
      require(persisted.provider(*persisted.current()).key == key, "The new provider key must persist.");
      require(persisted.policy(*persisted.current())["provider_ready"] == true, "A keyed provider is ready.");
      require(reopened.clearKey(id)["providers"][id]["key_set"] == false, "New provider keys can be cleared.");
    }

    requireReason("revision_conflict", "settings_revision_conflict", [&] {
      reopened.update(qws::Json::object(), std::string(16, '0'));
    });
    requireReason("invalid_argument", "settings_patch_invalid", [&] {
      reopened.update({{"unexpected", "diagnostic-secret-sentinel"}}, reopened.current()->revision);
    });
    requireReason("invalid_argument", "settings_endpoint_invalid", [&] {
      reopened.update({{"search", {{"searxng", {{"url", "https://diagnostic-secret-sentinel@example.test/"}}}}}},
                      reopened.current()->revision);
    });
    requireReason("invalid_argument", "provider_key_invalid", [&] {
      reopened.setKey("brave", "diagnostic-secret-sentinel with spaces");
    });
    requireReason("invalid_argument", "provider_unknown", [&] {
      reopened.clearKey("unknown");
    });
    reopened.reloadIfChanged();
    require(reopened.error().empty() && reopened.errorDetails().is_null(),
            "Valid restored files must clear both legacy and detailed diagnostics.");

    QFile invalidSchema(QDir(temporary.path()).filePath("state/settings.json"));
    const QByteArray invalidSettings = R"({"schema_version":1,"unexpected":"diagnostic-secret-sentinel"})";
    require(invalidSchema.open(QIODevice::WriteOnly) && invalidSchema.write(invalidSettings) == invalidSettings.size(),
            "Cannot write invalid schema fixture.");
    invalidSchema.close();
    reopened.reloadIfChanged();
    require(reopened.errorDetails()["reason"] == "settings_schema_invalid",
            "Settings schema failures must be distinct from JSON failures.");
    require(reopened.errorDetails().dump().find("diagnostic-secret-sentinel") == std::string::npos,
            "Settings schema diagnostics exposed saved data.");
    reopened.update(qws::Json::object(), reopened.current()->revision);
    reopened.reloadIfChanged();

    QFile invalidSecrets(QDir(temporary.path()).filePath("state/secrets.json"));
    require(invalidSecrets.open(QIODevice::WriteOnly) && invalidSecrets.write("{") == 1,
            "Cannot write invalid secrets fixture.");
    invalidSecrets.close();
    reopened.reloadIfChanged();
    require(reopened.errorDetails()["reason"] == "secrets_json_invalid",
            "Secret JSON errors must identify the secret store without exposing its contents.");
    const QByteArray malformedSecrets = R"({"schema_version":1,"keys":{"unknown":"diagnostic-secret-sentinel"}})";
    require(invalidSecrets.open(QIODevice::WriteOnly) && invalidSecrets.write(malformedSecrets) == malformedSecrets.size(),
            "Cannot write invalid secrets schema fixture.");
    invalidSecrets.close();
    reopened.reloadIfChanged();
    require(reopened.errorDetails()["reason"] == "secrets_schema_invalid",
            "Secret schema failures must be distinct from JSON failures.");
    require(reopened.errorDetails().dump().find("diagnostic-secret-sentinel") == std::string::npos,
            "Secret schema diagnostics exposed a provider key.");
    reopened.setKey("brave", "restored-provider-key");
    reopened.reloadIfChanged();
    require(reopened.errorDetails().is_null(), "Repairing provider keys must clear storage diagnostics.");

    const auto unchanged = reopened.current();
    require(QFile::remove(invalidSchema.fileName()) && QDir().mkpath(invalidSchema.fileName()),
            "Cannot prepare a directory occupying the settings file.");
    requireReason("internal_error", "settings_path_invalid", [&] {
      reopened.update({{"search", {{"provider", "auto"}}}}, unchanged->revision);
    });
    require(reopened.current() == unchanged, "Failed writes must not publish settings.");
    reopened.reloadIfChanged();
    require(reopened.errorDetails()["reason"] == "settings_path_invalid",
            "A directory occupying a settings file must have its own diagnostic.");
    require(QFile::remove(invalidSecrets.fileName()) && QDir().mkpath(invalidSecrets.fileName()),
            "Cannot prepare a directory occupying the provider key file.");
#ifdef _WIN32
    constexpr auto secretPathCode = "invalid_configuration";
#else
    constexpr auto secretPathCode = "internal_error";
#endif
    requireReason(secretPathCode, "secrets_path_invalid", [&] {
      reopened.setKey("brave", "replacement-provider-key");
    });
    require(reopened.current() == unchanged, "Rejected provider key writes must retain the previous snapshot.");

    QTemporaryDir blockedDirectory;
    require(blockedDirectory.isValid(), "Temporary directory required.");
    QFile blocker(QDir(blockedDirectory.path()).filePath("state"));
    require(blocker.open(QIODevice::WriteOnly) && blocker.write("occupied") == 8,
            "Cannot prepare a blocked settings directory.");
    blocker.close();
    qws::SettingsStore blocked({qws::defaultConfiguration(), blockedDirectory.path().toStdString()});
    requireReason("internal_error", "settings_directory_unavailable", [&] {
      blocked.update(qws::Json::object(), blocked.current()->revision);
    });
    requireReason("internal_error", "secrets_directory_unavailable", [&] {
      blocked.setKey("brave", "example-provider-key");
    });
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
  return 0;
}
