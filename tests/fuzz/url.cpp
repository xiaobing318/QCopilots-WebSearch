#include <QHostAddress>
#include <QUrl>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <qws/network.hpp>

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t *data, std::size_t size) {
  if (size > 16384)
    return 0;
  if (size >= 4) {
    const quint32 bits =
        (quint32(data[0]) << 24) | (quint32(data[1]) << 16) | (quint32(data[2]) << 8) | quint32(data[3]);
    const QHostAddress ipv4(bits);
    const QHostAddress mapped(QString("::ffff:") + ipv4.toString());
    if (qws::HttpClient::isPublicAddress(ipv4) != qws::HttpClient::isPublicAddress(mapped))
      std::abort();
  }
  try {
    const auto url = qws::normalizeHttpUrl(std::string(reinterpret_cast<const char *>(data), size));
    if (!url.isValid() || url.isRelative() || url.host().isEmpty() || !url.userInfo().isEmpty() ||
        url.hasFragment() || url.authority(QUrl::FullyEncoded).contains('@') || url.path().isEmpty() ||
        (url.scheme() != "http" && url.scheme() != "https"))
      std::abort();
    QHostAddress literal;
    if (literal.setAddress(url.host(QUrl::FullyDecoded)))
      (void)qws::HttpClient::isPublicAddress(literal);
    const auto encoded = url.toEncoded(QUrl::FullyEncoded).toStdString();
    if (encoded.size() <= 8192) {
      try {
        if (qws::normalizeHttpUrl(encoded) != url)
          std::abort();
      } catch (...) {
        std::abort();
      }
    }
  } catch (const qws::Error &error) {
    if (error.code != "invalid_argument")
      std::abort();
  }
  return 0;
}
