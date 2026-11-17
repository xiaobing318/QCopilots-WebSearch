#include <QString>
#include <QStringDecoder>
#include <QUrl>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <qws/html.hpp>

namespace {
bool hiddenCharacter(char32_t cp) {
  return cp == 0xad || cp == 0x200b || cp == 0x2060 || (cp >= 0x202a && cp <= 0x202e) ||
         (cp >= 0x2066 && cp <= 0x2069) || (cp >= 0xe0000 && cp <= 0xe007f) ||
         (QChar::category(cp) == QChar::Other_Control && cp != '\n' && cp != '\t');
}
QString content(const std::string &value, bool &valid) {
  // Extraction can remove controls before an embedded U+FEFF. It then becomes
  // the first scalar, but remains content rather than an input byte-order mark.
  QStringDecoder decoder(QStringDecoder::Utf8,
                         QStringConverter::Flag::ConvertInitialBom | QStringConverter::Flag::Stateless);
  const QString text = decoder(QByteArrayView(value.data(), qsizetype(value.size())));
  valid = !decoder.hasError() && text.toUtf8().toStdString() == value;
  return text;
}
} // namespace

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t *data, std::size_t size) {
  if (size == 0 || size > 1048578)
    return 0;
  static constexpr std::array<const char *, 9> types{
      "text/html; charset=utf-8",   "text/plain; charset=utf-8",        "text/html; charset=windows-1252",
      "text/html; charset=gb18030", "text/html; charset=utf-16le",      "",
      "application/json",           "text/html; charset=unknown-label", "application/xhtml+xml"};
  const std::string body(reinterpret_cast<const char *>(data + 1), size - 1);
  try {
    const auto result =
        qws::parseHtml(body, "https://example.invalid/base/document", types[data[0] % types.size()]);
    bool valid = false;
    const QString text = content(result.text, valid);
    if (!valid || text.toUcs4().size() > 200000 || result.title.size() > 8192 || !result.links.is_array() ||
        result.links.size() > 200)
      std::abort();
    for (const auto cp : text.toUcs4())
      if (hiddenCharacter(cp))
        std::abort();
    const QString title = content(result.title, valid);
    if (!valid || title.isEmpty())
      std::abort();
    for (const auto cp : title.toUcs4())
      if (hiddenCharacter(cp) || cp == '\n')
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
          url.authority(QUrl::FullyEncoded).contains('@') || url.hasFragment() ||
          (url.scheme() != "http" && url.scheme() != "https"))
        std::abort();
      const QString anchor = content(entry.at("text").get<std::string>(), valid);
      const auto line = entry.at("line").get<long long>();
      if (!valid || anchor.toUcs4().size() > 200 || anchor.contains('\n') || line < 1 || line > lines.size())
        std::abort();
    }
  } catch (const qws::Error &error) {
    // Complexity budgets truncate instead of failing.
    if (error.code != "unsupported_content")
      std::abort();
  }
  return 0;
}
