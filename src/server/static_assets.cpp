#include "static_assets.hpp"
#include <QFile>
#include <QResource>

static void initializeWebResources() {
  Q_INIT_RESOURCE(webui);
}

namespace qws {
void mountSettingsAssets(httplib::Server &server) {
  initializeWebResources();
  server.Get("/", [](const auto &, auto &res) { res.set_redirect("/settings/", 302); });
  const auto asset = [&server](const char *path, const char *resource, const char *type) {
    server.Get(path, [resource, type](const auto &, auto &res) {
      QFile file(QString::fromLatin1(resource));
      if (!file.open(QIODevice::ReadOnly)) {
        res.status = 404;
        return;
      }
      res.set_content(file.readAll().toStdString(), type);
    });
  };
  asset("/settings/", ":/web/index.html", "text/html; charset=utf-8");
  asset("/settings/assets/app.js", ":/web/assets/app.js", "text/javascript; charset=utf-8");
  asset("/settings/assets/app.css", ":/web/assets/app.css", "text/css; charset=utf-8");
  asset("/settings/assets/icon.svg", ":/web/assets/icon.svg", "image/svg+xml");
}
} // namespace qws
