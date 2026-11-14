#pragma once
#include <QByteArray>
#include <map>
#include <qws/common.hpp>
#include <vector>
namespace qws {
Json readSchema(const std::string &name);
Json defaultConfiguration();
void initializeConfiguration(const std::string &path);
void validateSchema(const Json &schema, const Json &value);
struct ClientIdentity {
  std::string id;
  QByteArray tokenHash;
};
struct Config {
  Json value;
  std::string baseDirectory;
  static Config load(const std::string &path);
  std::string path(const std::string &value) const;
  std::string secret(const Json &source, bool required) const;
  std::vector<ClientIdentity> clients() const;
};
std::string authenticate(const std::vector<ClientIdentity> &, const std::string &authorization);
} // namespace qws
