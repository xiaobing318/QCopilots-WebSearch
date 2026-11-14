#include <QString>
#include <QStringDecoder>
#include <QUrl>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <qws/html.hpp>

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t *data, std::size_t size) {
  if (size == 0 || size > 1048578)
    return 0;
  static constexpr std::array<const char *, 5> types{
      "text/html; charset=utf-8", "text/plain; charset=utf-8", "text/html; charset=windows-1252",
      "text/html; charset=gb18030", "text/html; charset=utf-16le"};
  const std::string body(reinterpret_cast<const char *>(data + 1), size - 1);
  try {
    const auto result =
        qws::parseHtml(body, "https://example.invalid/base/document", types[data[0] % types.size()]);
    // Extraction can remove controls before an embedded U+FEFF. It then becomes
    // the first scalar, but remains content rather than an input byte-order mark.
    QStringDecoder decoder(QStringDecoder::Utf8, QStringConverter::Flag::ConvertInitialBom |
                                                    QStringConverter::Flag::Stateless);
    const QString text = decoder(QByteArrayView(result.text.data(), qsizetype(result.text.size())));
    if (decoder.hasError() || text.toUtf8().toStdString() != result.text || text.toUcs4().size() > 200000 ||
        result.title.size() > 8192 || !result.links.is_array() || result.links.size() > 200)
      std::abort();
    const auto lines = text.split('\n');
    if (lines.size() > 10000)
      std::abort();
    for (const auto &line : lines)
      if (line.toUcs4().size() > 1000)
        std::abort();
    for (const auto &entry : result.links) {
      const auto value = entry.at("url").get<std::string>();
      const QUrl url(QString::fromStdString(value), QUrl::StrictMode);
      if (value.size() > 4096 || !url.isValid() || url.host().isEmpty() || !url.userInfo().isEmpty() ||
          url.authority(QUrl::FullyEncoded).contains('@') ||
          (url.scheme() != "http" && url.scheme() != "https"))
        std::abort();
    }
  } catch (const qws::Error &error) {
    if (error.code != "unsupported_content" && error.code != "response_too_large")
      std::abort();
  }
  return 0;
}
