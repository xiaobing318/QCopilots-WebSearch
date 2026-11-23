#include "test_support.hpp"
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QTcpServer>
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
    require(endpoint.at("allowed_hosts") ==
                qws::Json::array({"127.0.0.1:3181", "web-search.vastarmorai.com"}),
            "New configurations must accept the local and default public business Hosts.");
    require(!endpoint.contains("origins"), "Business configuration must not contain an Origin allowlist.");
    require(qws::businessMcpUrl(endpoint) == "https://web-search.vastarmorai.com/mcp",
            "New configurations must advertise the default public MCP endpoint.");
    qws::validateSchema(qws::readSchema("runtime"), config.value);
    auto removedBusinessOption = config.value;
    removedBusinessOption["business"]["origins"] = qws::Json::array();
    requireError("invalid_argument",
                 [&] { qws::validateSchema(qws::readSchema("runtime"), removedBusinessOption); });
    const auto requireConfigurationError = [](const qws::Json &value, const QString &path,
                                             const char *diagnostic) {
      require(QDir().mkpath(QFileInfo(path).absolutePath()), "Cannot create a configuration fixture directory.");
      QFile file(path);
      const auto bytes = QByteArray::fromStdString(value.dump());
      require(file.open(QIODevice::WriteOnly) && file.write(bytes) == bytes.size(),
              "Cannot write a configuration fixture.");
      file.close();
      try {
        (void)qws::Config::load(path.toStdString());
      } catch (const qws::Error &error) {
        require(error.code == "invalid_configuration", "Configuration failures must have a controlled error code.");
        require(std::string(error.what()).find(diagnostic) != std::string::npos,
                "Configuration failure used the wrong diagnostic.");
        return;
      }
      throw std::runtime_error("Expected a configuration error.");
    };
    const auto runtimePath = QDir(temporary.path()).filePath("runtime.json");
    auto previousRelease = removedBusinessOption;
    previousRelease["project_version"] = "0.2.0";
    requireConfigurationError(previousRelease, runtimePath, "different project release");
    requireConfigurationError(qws::Json{{"project_version", "0.2.0"}}, runtimePath, "different project release");
    requireConfigurationError(removedBusinessOption, runtimePath, "does not match its schema");
    for (const auto *layout : {"qcopilots-websearch-0.2.0/runtime.json", "qcopilots-websearch-0.2.0/service.json",
                               "QCopilots-WebSearch-0.2.0-win64/bin/data/runtime.json"})
      requireConfigurationError(removedBusinessOption, QDir(temporary.path()).filePath(layout),
                                "different project release");
    auto unversionedPackage = removedBusinessOption;
    unversionedPackage.erase("project_version");
    requireConfigurationError(unversionedPackage,
                              QDir(temporary.path()).filePath(
                                  QString::fromStdString("qcopilots-websearch-" +
                                      config.value.at("project_version").get<std::string>() + "/runtime.json")),
                              "different project release");
    for (const auto &invalidType : {qws::Json(), qws::Json::array(), qws::Json("invalid"),
                                    qws::Json(1), qws::Json(false)})
      requireConfigurationError(invalidType, runtimePath, "does not match its schema");
    for (const auto &invalidVersion : {qws::Json(), qws::Json::array(), qws::Json::object(),
                                       qws::Json(1), qws::Json(false)}) {
      auto invalidConfiguration = config.value;
      invalidConfiguration["project_version"] = invalidVersion;
      requireConfigurationError(invalidConfiguration, runtimePath, "does not match its schema");
    }
    auto unversionedPortable = config.value;
    unversionedPortable.erase("project_version");
    QFile portable(runtimePath);
    const auto portableBytes = QByteArray::fromStdString(unversionedPortable.dump());
    require(portable.open(QIODevice::WriteOnly) && portable.write(portableBytes) == portableBytes.size(),
            "Cannot write an unversioned portable configuration fixture.");
    portable.close();
    require(qws::Config::load(runtimePath.toStdString()).value == unversionedPortable,
            "An unversioned portable configuration outside a release directory must remain valid.");
    endpoint.erase("public_url");
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
    QTcpServer occupiedBusiness;
    require(occupiedBusiness.listen(QHostAddress::LocalHost, 0), "Cannot occupy a business port for the fixture.");
    auto assigned = config.value;
    assigned["business"]["port"] = occupiedBusiness.serverPort();
    assigned["business"]["allowed_hosts"][0] = "127.0.0.1:" + std::to_string(occupiedBusiness.serverPort());
    assigned["business"]["allowed_hosts"].push_back("search.example.test:443");
    qws::assignAvailableListenerPorts(assigned, QDir(temporary.path()).filePath("runtime.json"));
    require(assigned["business"]["port"] != occupiedBusiness.serverPort(),
            "An occupied business port must be replaced.");
    require(assigned["business"]["allowed_hosts"] ==
                qws::Json::array({"127.0.0.1:" + std::to_string(assigned["business"]["port"].get<int>()),
                                  "web-search.vastarmorai.com", "search.example.test:443"}),
            "Selecting an available port must update the local Host and preserve public Hosts.");
    require(assigned["business"]["public_url"] == config.value["business"]["public_url"],
            "Selecting a local port must preserve the public MCP endpoint.");
    require(assigned["control"]["allowed_hosts"] ==
                qws::Json::array({"127.0.0.1:" + std::to_string(assigned["control"]["port"].get<int>())}) &&
                assigned["control"]["origins"].empty(),
            "Control listener access must remain restricted to its local Host without browser Origins.");
    qws::validateSchema(qws::readSchema("runtime"), assigned);
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
