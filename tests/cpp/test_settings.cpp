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
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
  return 0;
}
