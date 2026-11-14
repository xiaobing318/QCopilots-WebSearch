#include <qws/config.hpp>
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QRandomGenerator>
#include <QSaveFile>
#include <QUrl>
#include <nlohmann/json-schema.hpp>
#include <set>

// Explicitly reference the resource initializer from this static library.
static void initSchemas() { Q_INIT_RESOURCE(schemas); }
namespace qws {
namespace {
Json readJson(const QString &path) {
  QFile file(path);
  if (!file.open(QIODevice::ReadOnly) || file.size() > 1024 * 1024)
    throw Error("invalid_configuration", "Cannot read bounded JSON configuration.");
  const auto bytes = file.readAll();
  auto result = Json::parse(bytes.constData(), bytes.constData()+bytes.size(), nullptr, false);
  if (result.is_discarded()) throw Error("invalid_configuration", "Configuration is not valid UTF-8 JSON.");
  return result;
}
}
Json readSchema(const std::string &name) {
  static const bool initialized = [] { initSchemas(); return true; }();
  (void)initialized;
  return readJson(QString::fromStdString(":/schemas/" + name + ".schema.json"));
}
void initializeConfiguration(const std::string &destination) {
  (void)readSchema("runtime");
  const QFileInfo info(QString::fromStdString(destination));
  if(info.exists()) { (void)Config::load(destination);return; }
  if(!QDir().mkpath(info.absolutePath())) throw Error("invalid_configuration","Cannot create configuration directory.");
  auto value=readJson(":/schemas/runtime.json");
  value["cache"]["directory"]="state";
  value["clients"]=Json::array({Json{{"id","local"},{"token_file","client.token"}}});
  const auto secretPath=info.absoluteDir().filePath("client.token");
  if(!QFileInfo::exists(secretPath)) {
    QByteArray bytes(32,Qt::Uninitialized);
    for(int i=0;i<32;++i) bytes[i]=char(QRandomGenerator::system()->generate()&255);
    QSaveFile file(secretPath);
    if(!file.open(QIODevice::WriteOnly)) throw Error("invalid_configuration","Cannot create credential file.");
#ifndef _WIN32
    file.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner);
#endif
    const auto encoded=bytes.toHex()+"\n";
    if(file.write(encoded)!=encoded.size()||!file.commit()) throw Error("invalid_configuration","Cannot save credential file.");
  }
  QSaveFile file(info.absoluteFilePath());
  if(!file.open(QIODevice::WriteOnly)) throw Error("invalid_configuration","Cannot create configuration file.");
#ifndef _WIN32
  file.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner);
#endif
  const auto json=QByteArray::fromStdString(value.dump(2)+"\n");
  if(file.write(json)!=json.size()||!file.commit()) throw Error("invalid_configuration","Cannot save configuration file.");
}
void validateSchema(const Json &schema, const Json &value) {
  try {
    nlohmann::json_schema::json_validator validator;
    validator.set_root_schema(schema); validator.validate(value);
  } catch (const std::exception &) {
    throw Error("invalid_argument", "Input does not match the versioned JSON schema.");
  }
}
Config Config::load(const std::string &path) {
  Config c{readJson(QString::fromStdString(path)), QFileInfo(QString::fromStdString(path)).absolutePath().toStdString()};
  try { validateSchema(readSchema("runtime"),c.value); }
  catch (const Error &) { throw Error("invalid_configuration", "Runtime configuration does not match its schema."); }
  const auto &provider = c.value["provider"];
  const QUrl url(QString::fromStdString(provider["url"].get<std::string>()),QUrl::StrictMode);
  if (!url.isValid() || url.host().isEmpty() || !url.userInfo().isEmpty() || url.hasQuery() ||
      url.hasFragment() || (url.scheme()!="http" && url.scheme()!="https"))
    throw Error("invalid_configuration", "Provider must be a fixed HTTP(S) endpoint without credentials or query.");
  if (c.value["business"]["host"]==c.value["control"]["host"] && c.value["business"]["port"]==c.value["control"]["port"])
    throw Error("invalid_configuration", "Business and control listeners need distinct ports.");
  return c;
}
std::string Config::path(const std::string &value) const {
  if (value.empty()) return {};
  return QFileInfo(QDir(QString::fromStdString(baseDirectory)),QString::fromStdString(value)).absoluteFilePath().toStdString();
}
std::string Config::secret(const Json &source, bool required) const {
  QByteArray token;
  if (source.contains("token_env")) token=qgetenv(source["token_env"].get<std::string>().c_str());
  else if(source.contains("token_file")) {
    QFile file(QString::fromStdString(path(source["token_file"])));
    const QFileInfo info(file);
    if (info.isSymLink() || !file.open(QIODevice::ReadOnly) || file.size()>8192)
      throw Error("invalid_configuration", "Credential file is inaccessible or too large.");
#ifndef _WIN32
    if (file.permissions() & (QFileDevice::ReadGroup|QFileDevice::WriteGroup|QFileDevice::ExeGroup|
                            QFileDevice::ReadOther|QFileDevice::WriteOther|QFileDevice::ExeOther))
      throw Error("invalid_configuration", "Credential file must be accessible only to its owner.");
#endif
    token=file.readAll();
    if(token.endsWith('\n')) token.chop(1);
    if(token.endsWith('\r')) token.chop(1);
  }
  if ((required && token.size()<32) || token.size()>8192 ||
      ((!source.contains("token_env") && !source.contains("token_file")) ? false : token.isEmpty()))
    throw Error("invalid_configuration", "Client credentials need at least 32 characters and configured credentials must be present.");
  for(unsigned char c : token) if(c<0x21 || c>0x7e)
    throw Error("invalid_configuration", "Credentials must contain visible ASCII only.");
  return token.toStdString();
}
std::vector<ClientIdentity> Config::clients() const {
  std::vector<ClientIdentity> result;
  std::set<std::string> ids;
  std::set<QByteArray> hashes;
  for(const auto &c : value["clients"]) {
    const auto id=c["id"].get<std::string>();
    const auto hash=QCryptographicHash::hash(QByteArray::fromStdString(secret(c,true)),QCryptographicHash::Sha256);
    if(!ids.insert(id).second || !hashes.insert(hash).second)
      throw Error("invalid_configuration", "Client identities and credentials must be unique.");
    result.push_back({id,hash});
  }
  return result;
}
std::string authenticate(const std::vector<ClientIdentity> &clients, const std::string &authorization) {
  if(!authorization.starts_with("Bearer ") || authorization.size()>8200)
    throw Error("unauthorized", "A valid bearer credential is required.",401);
  const auto supplied=QCryptographicHash::hash(QByteArray::fromStdString(authorization.substr(7)),QCryptographicHash::Sha256);
  std::string identity;
  for(const auto &c:clients) {
    unsigned char diff=0;
    for(qsizetype i=0;i<supplied.size();++i) diff|=static_cast<unsigned char>(supplied[i]^c.tokenHash[i]);
    if(diff==0) identity=c.id;
  }
  if(identity.empty()) throw Error("unauthorized", "A valid bearer credential is required.",401);
  return identity;
}
}
