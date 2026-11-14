#pragma once
#include <qws/common.hpp>
#include <QByteArray>
#include <map>
#include <vector>
namespace qws {
Json readSchema(const std::string &name);
void initializeConfiguration(const std::string &path);
void validateSchema(const Json &schema, const Json &value);
struct ClientIdentity { std::string id; QByteArray tokenHash; };
struct Config {
  Json value;
  std::string baseDirectory;
  static Config load(const std::string &path);
  std::string path(const std::string &value) const;
  std::string secret(const Json &source, bool required) const;
  std::vector<ClientIdentity> clients() const;
};
std::string authenticate(const std::vector<ClientIdentity> &, const std::string &authorization);
}
