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
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
  return 0;
}
