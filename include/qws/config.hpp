#pragma once
#include <QByteArray>
#include <QString>
#include <map>
#include <qws/common.hpp>
#include <vector>
namespace qws {
// Bundled schemas are parsed and compiled once. The returned reference stays
// valid for the process lifetime, and validateSchema reuses its validator.
const Json &readSchema(const std::string &name);
// contracts/v1/tools.json: {"instructions": string, "tools": [{name, title,
// description, annotations}]} in tools/list order.
const Json &toolCatalog();
Json defaultConfiguration();
Json defaultSettings();
// A supplied port is the actual business port, including internal port-zero binds.
std::string businessMcpUrl(const Json &business, int boundPort = -1);
void assignAvailableListenerPorts(Json &configuration, const QString &configPath);
void initializeConfiguration(const std::string &path, bool selectAvailablePorts = false,
                             const QString &stateDirectory = {});
void setClientToken(const std::string &path, const std::string &id, const QByteArray &token);
// Throws invalid_argument naming the first failing JSON pointer and reason.
void validateSchema(const Json &schema, const Json &value);
struct ClientIdentity {
  std::string id;
  QByteArray tokenHash;
  std::string principal = {};
};
struct Config {
  Json value;
  std::string baseDirectory;
  std::string sourcePath = {};
  static Config load(const std::string &path);
  std::string path(const std::string &value) const;
  std::string secret(const Json &source, bool required, int minimumLength = 32) const;
  std::vector<ClientIdentity> clients() const;
};
std::string authenticate(const std::vector<ClientIdentity> &, const std::string &authorization);
} // namespace qws
