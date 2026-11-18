#include <QProcess>
#include <QUrl>
#include <httplib.h>
#include <qws/admin.hpp>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <shellapi.h>
#endif

namespace qws {
std::string settingsUrl(const Config &runtime) {
  auto admin = adminConfiguration(runtime);
  if (!admin.enabled)
    throw Error("invalid_configuration", "Administration is disabled.");
  httplib::Client client(admin.host, admin.port);
  client.set_connection_timeout(2);
  client.set_read_timeout(2);
  client.set_write_timeout(2);
  auto response = client.Post("/admin/api/v1/login-links",
                              httplib::Headers{{"Authorization", "Bearer " + admin.token}},
                              "", "application/json");
  if (!response || response->status != 200 || response->body.size() > 4096)
    throw Error("provider_unavailable", "The administration listener is unavailable.", 503);
  const auto data = parseBoundedJson(response->body);
  if (!data.is_object() || !data.contains("url") || !data["url"].is_string())
    throw Error("provider_unavailable", "The administration listener returned an invalid link.", 503);
  const auto url = data["url"].get<std::string>();
  const auto prefix = admin.origin + "/settings/#login=";
  if (!url.starts_with(prefix) || url.size() != prefix.size() + 64 ||
      url.find_first_not_of("0123456789abcdef", prefix.size()) != std::string::npos)
    throw Error("provider_unavailable", "The administration listener returned an invalid link.", 503);
  return url;
}

void openSettingsUrl(const std::string &url) {
#ifdef _WIN32
  const auto target = QString::fromStdString(url).toStdWString();
  if (reinterpret_cast<INT_PTR>(ShellExecuteW(nullptr, L"open", target.c_str(), nullptr, nullptr, SW_SHOWNORMAL)) <= 32)
#else
  if (!QProcess::startDetached("xdg-open", {QString::fromStdString(url)}))
#endif
    throw Error("invalid_configuration", "Cannot open the settings page.");
}
} // namespace qws
