#include "html_dom.hpp"
#include <QChar>
#include <QString>
#include <QUrl>
#include <algorithm>
#include <array>
#include <charconv>
#include <lexbor/encoding/encoding.h>
#include <optional>
#include <qws/html.hpp>
#include <string_view>
#include <unordered_map>
#include <utility>
#include <vector>
namespace qws {
namespace {
using namespace html_dom;
// Encoded bytes read from one response. Larger inputs are cut and disclosed.
constexpr std::size_t InputLimit = 1024 * 1024;
constexpr std::size_t PrescanLimit = 4096;
constexpr std::size_t TextLimit = 200000, LineLimit = 10000, LineWidth = 1000;
constexpr std::size_t LinkLimit = 200, LinkTextLimit = 200, TitleLimit = 2048;

[[noreturn]] void encodingError() {
  throw Error("unsupported_content", "Unsupported charset or invalid encoded text.", 415);
}
std::optional<QString> charsetParameter(const QString &input) {
  qsizetype i = 0;
  while (i < input.size()) {
    while (i < input.size() && (input[i].isSpace() || input[i] == ';'))
      ++i;
    const auto begin = i;
    while (i < input.size() && input[i] != '=' && input[i] != ';')
      ++i;
    const auto key = input.mid(begin, i - begin).trimmed();
    if (i == input.size())
      break;
    if (input[i] == ';') {
      ++i;
      continue;
    }
    ++i;
    while (i < input.size() && input[i].isSpace())
      ++i;
    QString value;
    bool valid = true;
    if (i < input.size() && (input[i] == '"' || input[i] == '\'')) {
      const auto quote = input[i++];
      bool closed = false;
      while (i < input.size()) {
        auto c = input[i++];
        if (c == quote) {
          closed = true;
          break;
        }
        if (c == '\\' && i < input.size())
          c = input[i++];
        value.append(c);
      }
      valid = closed;
      while (i < input.size() && input[i] != ';') {
        if (!input[i].isSpace())
          valid = false;
        ++i;
      }
    } else {
      const auto start = i;
      while (i < input.size() && input[i] != ';')
        ++i;
      value = input.mid(start, i - start).trimmed();
    }
    if (key.compare("charset", Qt::CaseInsensitive) == 0)
      return valid ? value.trimmed() : QString();
  }
  return std::nullopt;
}

std::optional<std::string> earlyMetaCharset(std::string_view body) {
  Document doc(lxb_html_document_create(), lxb_html_document_destroy);
  if (!doc)
    return std::nullopt;
  lxb_html_document_scripting_set(doc.get(), true);
  try {
    parseGuarded(doc.get(), body.substr(0, PrescanLimit));
  } catch (const Error &) {
    return std::nullopt;
  }
  std::vector<Node *> pending{lxb_dom_interface_node(doc.get())};
  for (std::size_t visited = 0; !pending.empty() && visited < NodeBudget; ++visited) {
    auto *n = pending.back();
    pending.pop_back();
    if (n->type == LXB_DOM_NODE_TYPE_ELEMENT) {
      switch (n->local_name) {
      case LXB_TAG_SCRIPT:
      case LXB_TAG_STYLE:
      case LXB_TAG_NOSCRIPT:
      case LXB_TAG_TEMPLATE:
        continue;
      default:
        break;
      }
      if (n->local_name == LXB_TAG_META) {
        const auto direct = trimAscii(attribute(n, "charset"));
        if (!direct.empty())
          return std::string(direct);
        if (equalsAscii(trimAscii(attribute(n, "http-equiv")), "content-type")) {
          const auto content = attribute(n, "content");
          if (auto label = charsetParameter(QString::fromUtf8(content.data(), qsizetype(content.size())));
              label && !label->isEmpty())
            return label->toStdString();
        }
      }
    }
    for (auto *c = n->last_child; c; c = c->prev)
      pending.push_back(c);
  }
  return std::nullopt;
}
struct EncodingChoice {
  const lxb_encoding_data_t *data;
  std::size_t bomBytes = 0;
};
const lxb_encoding_data_t *usable(const lxb_encoding_data_t *data) {
  if (!data || !data->decode || data->encoding == LXB_ENCODING_DEFAULT ||
      data->encoding == LXB_ENCODING_AUTO || data->encoding == LXB_ENCODING_UNDEFINED ||
      data->encoding == LXB_ENCODING_REPLACEMENT)
    return nullptr;
  return data;
}
// BOM, then a supported HTTP charset, then a supported early meta charset, then
// UTF-8. Unknown labels are ignored as browsers do.
EncodingChoice chooseEncoding(std::string_view bytes, const std::string &contentType, bool html) {
  if (bytes.starts_with(std::string_view("\xff\xfe\0\0", 4)) ||
      bytes.starts_with(std::string_view("\0\0\xfe\xff", 4)))
    encodingError();
  if (bytes.starts_with(std::string_view("\xef\xbb\xbf", 3)))
    return {lxb_encoding_data(LXB_ENCODING_UTF_8), 3};
  if (bytes.starts_with(std::string_view("\xff\xfe", 2)))
    return {lxb_encoding_data(LXB_ENCODING_UTF_16LE), 2};
  if (bytes.starts_with(std::string_view("\xfe\xff", 2)))
    return {lxb_encoding_data(LXB_ENCODING_UTF_16BE), 2};
  if (auto label = charsetParameter(QString::fromStdString(contentType)); label && !label->isEmpty()) {
    const auto name = label->trimmed().toLatin1();
    if (const auto *data = usable(lxb_encoding_data_by_pre_name(
            reinterpret_cast<const lxb_char_t *>(name.constData()), size_t(name.size()))))
      return {data, 0};
  }
  if (html)
    if (const auto label = earlyMetaCharset(bytes))
      if (const auto *data = usable(lxb_encoding_data_prescan_validate(
              reinterpret_cast<const lxb_char_t *>(label->data()), label->size())))
        return {data, 0};
  return {lxb_encoding_data(LXB_ENCODING_UTF_8), 0};
}
// Decodes to UTF-8. Malformed input becomes U+FFFD. More than 1% replacement
// characters means the charset is wrong or the content is binary.
std::string decode(std::string_view bytes, EncodingChoice choice, bool truncated) {
  bytes.remove_prefix(std::min(bytes.size(), choice.bomBytes));
  // A value outside Unicode identifies decoder replacements exactly.
  static const lxb_codepoint_t replacement = 0x110000;
  std::array<lxb_codepoint_t, 4096> buffer{};
  lxb_encoding_decode_t state{};
  if (lxb_encoding_decode_init(&state, choice.data, buffer.data(), buffer.size()) != LXB_STATUS_OK ||
      lxb_encoding_decode_replace_set(&state, &replacement, 1) != LXB_STATUS_OK)
    encodingError();
  std::string out;
  out.reserve(bytes.size());
  std::size_t scalars = 0, replaced = 0;
  const auto drain = [&] {
    const auto count = lxb_encoding_decode_buf_used(&state);
    for (size_t i = 0; i < count; ++i) {
      char32_t cp = buffer[i];
      if (cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff)) {
        cp = 0xfffd;
        ++replaced;
      }
      appendUtf8(out, cp);
      ++scalars;
    }
    lxb_encoding_decode_buf_used_set(&state, 0);
    return count;
  };
  const auto *cursor = reinterpret_cast<const lxb_char_t *>(bytes.data());
  const auto *end = cursor + bytes.size();
  for (;;) {
    const auto *before = cursor;
    const auto status = choice.data->decode(&state, &cursor, end);
    if (status != LXB_STATUS_OK && status != LXB_STATUS_SMALL_BUFFER && status != LXB_STATUS_CONTINUE)
      encodingError();
    const auto produced = drain();
    if (status == LXB_STATUS_SMALL_BUFFER) {
      if (cursor == before && produced == 0)
        encodingError();
      continue;
    }
    if (cursor != end)
      encodingError();
    // A sequence cut by the input limit is dropped. A sequence cut by the end of
    // a complete response is malformed and becomes a replacement.
    if (!(truncated && status == LXB_STATUS_CONTINUE)) {
      if (lxb_encoding_decode_finish(&state) != LXB_STATUS_OK)
        encodingError();
      drain();
    }
    break;
  }
  if (replaced * 100 > scalars)
    encodingError();
  return out;
}

enum class Kind { Html, Text };
bool binaryByte(unsigned char c) {
  return c <= 0x08 || c == 0x0b || (c >= 0x0e && c <= 0x1a) || (c >= 0x1c && c <= 0x1f);
}
// WHATWG-style sniffing for a response without a Content-Type.
Kind sniff(std::string_view body) {
  auto s = body;
  if (s.starts_with("\xef\xbb\xbf"))
    s.remove_prefix(3);
  else if (s.starts_with("\xff\xfe") || s.starts_with("\xfe\xff"))
    return Kind::Text;
  while (!s.empty() && (s[0] == ' ' || s[0] == '\t' || s[0] == '\n' || s[0] == '\r' || s[0] == '\f'))
    s.remove_prefix(1);
  if (s.starts_with("<!--"))
    return Kind::Html;
  constexpr std::string_view tags[] = {
      "<!doctype html", "<html", "<head",  "<script", "<iframe", "<h1",   "<div", "<font",
      "<table",         "<a",    "<style", "<title",  "<b",      "<body", "<br",  "<p"};
  for (const auto tag : tags)
    if (startsWithAscii(s, tag) && s.size() > tag.size() && (s[tag.size()] == ' ' || s[tag.size()] == '>'))
      return Kind::Html;
  for (const unsigned char c : body.substr(0, 1024))
    if (binaryByte(c))
      unsupported("Response content is not text.");
  return Kind::Text;
}
Kind classify(const std::string &contentType, std::string_view body) {
  std::string mime;
  for (const char c : trimAscii(std::string_view(contentType).substr(0, contentType.find(';'))))
    mime.push_back(lowerAscii(c));
  if (mime.empty())
    return sniff(body);
  if (mime == "text/html" || mime == "application/xhtml+xml")
    return Kind::Html;
  if (mime.starts_with("text/"))
    return Kind::Text;
  if (mime.starts_with("application/")) {
    const auto subtype = std::string_view(mime).substr(12);
    if (subtype == "json" || subtype == "xml" || subtype.ends_with("+json") || subtype.ends_with("+xml"))
      return Kind::Text;
  }
  unsupported("Only HTML, text, JSON and XML content are supported.");
}

bool styleHides(std::string_view style) {
  while (!style.empty()) {
    const auto semicolon = style.find(';');
    const auto declaration = style.substr(0, semicolon);
    style = semicolon == std::string_view::npos ? std::string_view() : style.substr(semicolon + 1);
    const auto colon = declaration.find(':');
    if (colon == std::string_view::npos)
      continue;
    const auto key = trimAscii(declaration.substr(0, colon)),
               value = trimAscii(declaration.substr(colon + 1));
    if ((equalsAscii(key, "display") && startsWithAscii(value, "none")) ||
        (equalsAscii(key, "visibility") && startsWithAscii(value, "hidden")))
      return true;
  }
  return false;
}
// ARIA landmark roles mark the same page chrome as nav, header and footer.
// The attribute is a whitespace-separated list, so every token is checked.
bool landmarkHides(std::string_view roles, bool sectioned) {
  constexpr std::string_view space = " \t\n\f\r";
  for (auto start = roles.find_first_not_of(space); start != std::string_view::npos;
       start = roles.find_first_not_of(space)) {
    roles.remove_prefix(start);
    const auto token = roles.substr(0, roles.find_first_of(space));
    roles.remove_prefix(token.size());
    if (equalsAscii(token, "navigation") ||
        (!sectioned && (equalsAscii(token, "banner") || equalsAscii(token, "contentinfo"))))
      return true;
  }
  return false;
}
// Page-level header and footer landmarks are boilerplate. Inside article,
// section, main or aside they hold the content's own title, byline and notes.
bool hidden(Node *n, bool sectioned) {
  if (n->type != LXB_DOM_NODE_TYPE_ELEMENT)
    return false;
  switch (n->local_name) {
  case LXB_TAG_SCRIPT:
  case LXB_TAG_STYLE:
  case LXB_TAG_NAV:
  case LXB_TAG_TEMPLATE:
  case LXB_TAG_NOSCRIPT:
  case LXB_TAG_HEAD:
  case LXB_TAG_SVG:
  case LXB_TAG_IFRAME:
  case LXB_TAG_OBJECT:
  case LXB_TAG_EMBED:
  case LXB_TAG_CANVAS:
  case LXB_TAG_VIDEO:
  case LXB_TAG_AUDIO:
    return true;
  case LXB_TAG_HEADER:
  case LXB_TAG_FOOTER:
    if (!sectioned)
      return true;
    break;
  default:
    break;
  }
  if (hasAttribute(n, "hidden") && !equalsAscii(trimAscii(attribute(n, "hidden")), "until-found"))
    return true;
  if (equalsAscii(trimAscii(attribute(n, "aria-hidden")), "true"))
    return true;
  if (landmarkHides(attribute(n, "role"), sectioned))
    return true;
  return styleHides(attribute(n, "style"));
}
bool sectioning(Node *n) {
  switch (n->local_name) {
  case LXB_TAG_ARTICLE:
  case LXB_TAG_SECTION:
  case LXB_TAG_MAIN:
  case LXB_TAG_ASIDE:
    return true;
  default:
    return false;
  }
}
bool preformatted(Node *n) {
  switch (n->local_name) {
  case LXB_TAG_PRE:
  case LXB_TAG_LISTING:
  case LXB_TAG_PLAINTEXT:
  case LXB_TAG_TEXTAREA:
  case LXB_TAG_XMP:
    return true;
  default:
    return false;
  }
}
int headingLevel(Node *n) {
  switch (n->local_name) {
  case LXB_TAG_H1:
    return 1;
  case LXB_TAG_H2:
    return 2;
  case LXB_TAG_H3:
    return 3;
  case LXB_TAG_H4:
    return 4;
  case LXB_TAG_H5:
    return 5;
  case LXB_TAG_H6:
    return 6;
  default:
    return 0;
  }
}
bool block(Node *n) {
  if (headingLevel(n))
    return true;
  switch (n->local_name) {
  case LXB_TAG_ADDRESS:
  case LXB_TAG_ARTICLE:
  case LXB_TAG_ASIDE:
  case LXB_TAG_BLOCKQUOTE:
  case LXB_TAG_BR:
  case LXB_TAG_CAPTION:
  case LXB_TAG_CENTER:
  case LXB_TAG_DD:
  case LXB_TAG_DETAILS:
  case LXB_TAG_DIALOG:
  case LXB_TAG_DIV:
  case LXB_TAG_DL:
  case LXB_TAG_DT:
  case LXB_TAG_FIELDSET:
  case LXB_TAG_FIGCAPTION:
  case LXB_TAG_FIGURE:
  case LXB_TAG_FOOTER:
  case LXB_TAG_FORM:
  case LXB_TAG_HEADER:
  case LXB_TAG_HGROUP:
  case LXB_TAG_HR:
  case LXB_TAG_LEGEND:
  case LXB_TAG_LI:
  case LXB_TAG_LISTING:
  case LXB_TAG_MAIN:
  case LXB_TAG_MENU:
  case LXB_TAG_OL:
  case LXB_TAG_P:
  case LXB_TAG_PLAINTEXT:
  case LXB_TAG_PRE:
  case LXB_TAG_SEARCH:
  case LXB_TAG_SECTION:
  case LXB_TAG_SUMMARY:
  case LXB_TAG_TABLE:
  case LXB_TAG_TBODY:
  case LXB_TAG_TEXTAREA:
  case LXB_TAG_TFOOT:
  case LXB_TAG_THEAD:
  case LXB_TAG_TR:
  case LXB_TAG_UL:
  case LXB_TAG_XMP:
    return true;
  default:
    return false;
  }
}
bool listContainer(Node *n) {
  return n->local_name == LXB_TAG_UL || n->local_name == LXB_TAG_OL || n->local_name == LXB_TAG_MENU;
}
struct List {
  bool ordered;
  long long next;
};
long long listStart(Node *n) {
  const auto value = trimAscii(attribute(n, "start"));
  long long start = 1;
  const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), start);
  if (error != std::errc() || end != value.data() + value.size())
    return 1;
  return std::clamp<long long>(start, 0, 1000000000);
}
std::string listMarker(std::vector<List> &lists) {
  std::string marker(2 * std::min<std::size_t>(lists.empty() ? 0 : lists.size() - 1, 8), ' ');
  if (!lists.empty() && lists.back().ordered)
    marker += std::to_string(lists.back().next++) + ". ";
  else
    marker += "- ";
  return marker;
}

// Bounded readable text with light Markdown markers. Markers and separators are
// written only when visible content follows them on the same line.
struct Output {
  std::string value, prefix, whitespace;
  std::size_t scalars = 0, lineScalars = 0, lines = 1;
  bool pendingSpace = false, pendingBoundary = false, pendingCell = false, full = false;
  std::string *capture = nullptr;
  std::size_t captureScalars = 0, captureLine = 0;
  bool captureGap = false;

  std::size_t nextLine() const {
    return lines + (pendingBoundary ? 1 : 0);
  }
  void gap() {
    if (capture && !capture->empty())
      captureGap = true;
  }
  void startCapture(std::string *target) {
    capture = target;
    captureScalars = captureLine = 0;
    captureGap = false;
  }
  void wrap() {
    pendingSpace = false;
    pendingBoundary = true;
    lineScalars = 0;
  }
  void boundary() {
    pendingSpace = pendingCell = false;
    whitespace.clear();
    gap();
    if (full || value.empty() || pendingBoundary)
      return;
    // A trailing block boundary is discarded, so it must not consume a
    // line or report truncation until there is more visible content.
    pendingBoundary = true;
    lineScalars = 0;
  }
  void cell() {
    pendingCell = true;
    gap();
  }
  void raw(char32_t cp) {
    if (full)
      return;
    if (lineScalars >= LineWidth)
      wrap();
    if (pendingBoundary) {
      if (lines >= LineLimit || scalars + 1 >= TextLimit) {
        full = true;
        return;
      }
      value.push_back('\n');
      ++scalars;
      ++lines;
      pendingBoundary = false;
    }
    if (scalars >= TextLimit) {
      full = true;
      return;
    }
    appendUtf8(value, cp);
    ++scalars;
    ++lineScalars;
  }
  void content(char32_t cp) {
    if (full)
      return;
    if (pendingSpace && lineScalars + 1 >= LineWidth)
      wrap();
    const bool lineStart = pendingBoundary || lineScalars == 0;
    const std::string_view lead = lineStart      ? std::string_view(prefix)
                                  : pendingCell  ? std::string_view(" | ")
                                  : pendingSpace ? std::string_view(" ")
                                                 : std::string_view();
    // Do not leave a marker without the content it introduces.
    if (scalars + lead.size() + whitespace.size() + 1 + (pendingBoundary ? 1 : 0) > TextLimit ||
        (pendingBoundary && lines >= LineLimit)) {
      full = true;
      return;
    }
    for (const char c : lead)
      raw(static_cast<unsigned char>(c));
    if (lineStart)
      prefix.clear();
    pendingSpace = pendingCell = false;
    for (const char c : whitespace)
      raw(static_cast<unsigned char>(c));
    whitespace.clear();
    raw(cp);
    if (full || !capture || captureScalars >= LinkTextLimit)
      return;
    if (capture->empty()) {
      captureLine = lines;
    } else if (captureGap) {
      if (captureScalars + 1 >= LinkTextLimit) {
        captureScalars = LinkTextLimit;
        return;
      }
      capture->push_back(' ');
      ++captureScalars;
    }
    captureGap = false;
    appendUtf8(*capture, cp);
    ++captureScalars;
  }
  void text(std::string_view s, bool preserve) {
    for (std::size_t i = 0; i < s.size() && !full;) {
      const char32_t cp = nextScalar(s, i);
      if (preserve && (cp == '\r' || cp == '\n')) {
        if (cp == '\r' && i < s.size() && s[i] == '\n')
          ++i;
        boundary();
        continue;
      }
      if (isSpace(cp)) {
        // Preformatted text keeps indentation and inner runs, never trailing space.
        if (preserve) {
          if (whitespace.size() < LineWidth)
            whitespace.push_back(cp == '\t' ? '\t' : ' ');
        } else {
          pendingSpace = lineScalars > 0;
        }
        gap();
        continue;
      }
      if (dropped(cp))
        continue;
      content(cp);
    }
  }
};

struct Facts {
  Node *base = nullptr, *heading = nullptr;
  std::string_view ogTitle;
};
// The first base with href (as browsers use), og:title and the first h1.
Facts documentFacts(Node *root) {
  Facts facts;
  struct Item {
    Node *node;
    std::size_t depth;
  };
  std::vector<Item> pending{{root, 0}};
  for (std::size_t visited = 0; !pending.empty() && visited < 2 * NodeBudget; ++visited) {
    const auto [n, depth] = pending.back();
    pending.pop_back();
    if (depth > DepthBudget)
      continue;
    if (n->type == LXB_DOM_NODE_TYPE_ELEMENT) {
      if (n->local_name == LXB_TAG_TEMPLATE || n->local_name == LXB_TAG_NOSCRIPT)
        continue;
      if (n->local_name == LXB_TAG_BASE && !facts.base && hasAttribute(n, "href"))
        facts.base = n;
      if (n->local_name == LXB_TAG_META && facts.ogTitle.empty() &&
          equalsAscii(trimAscii(attribute(n, "property")), "og:title"))
        facts.ogTitle = attribute(n, "content");
      if (n->local_name == LXB_TAG_H1 && !facts.heading)
        facts.heading = n;
    }
    for (auto *c = n->last_child; c; c = c->prev)
      pending.push_back({c, depth + 1});
  }
  return facts;
}
// Prefer the visible main element, then a single top-level article. Several
// articles are a listing or thread, so the whole body is read instead.
Node *readableRoot(Node *body) {
  Node *main = nullptr, *article = nullptr;
  std::size_t articles = 0;
  struct Item {
    Node *node;
    std::size_t depth;
    bool sectioned, inArticle;
  };
  std::vector<Item> pending{{body, 0, false, false}};
  for (std::size_t visited = 0; !pending.empty() && visited < NodeBudget; ++visited) {
    const auto f = pending.back();
    pending.pop_back();
    if (f.depth > DepthBudget || hidden(f.node, f.sectioned))
      continue;
    if (f.node->type == LXB_DOM_NODE_TYPE_ELEMENT) {
      if (f.node->local_name == LXB_TAG_MAIN && !main)
        main = f.node;
      if (f.node->local_name == LXB_TAG_ARTICLE && !f.inArticle && ++articles == 1)
        article = f.node;
    }
    const bool sectioned = f.sectioned || sectioning(f.node);
    const bool inArticle = f.inArticle || f.node->local_name == LXB_TAG_ARTICLE;
    for (auto *c = f.node->last_child; c; c = c->prev)
      pending.push_back({c, f.depth + 1, sectioned, inArticle});
  }
  return main ? main : articles == 1 ? article : body;
}
struct Link {
  std::string url, text;
  std::size_t line;
};
void extractHtml(const std::string &html, const std::string &url, HtmlDocument &out, Output &text) {
  Document doc(lxb_html_document_create(), lxb_html_document_destroy);
  if (!doc)
    unsupported("HTML parsing failed.");
  if (!parseGuarded(doc.get(), html))
    out.truncated = true;
  const auto facts = documentFacts(lxb_dom_interface_node(doc.get()));
  size_t length = 0;
  const auto *title = lxb_html_document_title(doc.get(), &length);
  out.title = inlineText(view(title, length), TitleLimit);
  if (out.title.empty())
    out.title = inlineText(facts.ogTitle, TitleLimit);
  if (out.title.empty() && facts.heading)
    out.title = inlineText(textContent(facts.heading, 4 * TitleLimit + 64), TitleLimit);
  QUrl page(QString::fromStdString(url), QUrl::TolerantMode);
  page.setFragment(QString());
  const auto self = page.toString(QUrl::FullyEncoded).toStdString();
  QUrl base = page;
  if (facts.base)
    if (const auto target = resolveHttp(page, attribute(facts.base, "href")))
      base = QUrl(QString::fromStdString(*target), QUrl::StrictMode);
  auto *body = lxb_dom_interface_node(lxb_html_document_body_element(doc.get()));
  if (!body) {
    if (out.truncated)
      return;
    unsupported("HTML has no readable body.");
  }
  auto *root = readableRoot(body);
  struct Frame {
    Node *node;
    std::size_t depth;
    bool exit = false, pre = false, sectioned = false, captures = false;
  };
  std::vector<Frame> stack{
      {root, 0, false, false, root->local_name == LXB_TAG_MAIN || root->local_name == LXB_TAG_ARTICLE}};
  std::vector<List> lists;
  std::vector<Link> links;
  std::unordered_map<std::string, std::size_t> byUrl;
  std::string anchor;
  std::size_t anchorLink = 0, visited = 0;
  while (!stack.empty() && !text.full) {
    auto f = stack.back();
    stack.pop_back();
    auto *n = f.node;
    if (f.exit) {
      if (block(n))
        text.boundary();
      if (headingLevel(n) || n->local_name == LXB_TAG_LI)
        text.prefix.clear();
      if (listContainer(n) && !lists.empty())
        lists.pop_back();
      if (f.captures) {
        if (!anchor.empty()) {
          links[anchorLink].text = anchor;
          links[anchorLink].line = text.captureLine;
        }
        text.capture = nullptr;
      }
      continue;
    }
    if (++visited > NodeBudget) {
      out.truncated = true;
      break;
    }
    if (f.depth > DepthBudget) {
      out.truncated = true;
      continue;
    }
    if (hidden(n, f.sectioned))
      continue;
    if (n->type == LXB_DOM_NODE_TYPE_TEXT) {
      const auto &d = lxb_dom_interface_character_data(n)->data;
      text.text(view(d.data, d.length), f.pre);
      continue;
    }
    if (n->type != LXB_DOM_NODE_TYPE_ELEMENT)
      continue;
    Frame exit{n, f.depth, true, f.pre, f.sectioned};
    if (block(n))
      text.boundary();
    if (const int level = headingLevel(n)) {
      text.prefix.assign(std::size_t(level), '#');
      text.prefix.push_back(' ');
    }
    switch (n->local_name) {
    case LXB_TAG_UL:
    case LXB_TAG_MENU:
      lists.push_back({false, 0});
      break;
    case LXB_TAG_OL:
      lists.push_back({true, listStart(n)});
      break;
    case LXB_TAG_LI:
      text.prefix = listMarker(lists);
      break;
    case LXB_TAG_TD:
    case LXB_TAG_TH:
      text.cell();
      break;
    case LXB_TAG_A: {
      auto target = resolveHttp(base, attribute(n, "href"));
      if (!target || *target == self)
        break;
      std::optional<std::size_t> index;
      if (const auto it = byUrl.find(*target); it != byUrl.end()) {
        // A repeated URL keeps its first position unless it had no text yet.
        if (links[it->second].text.empty())
          index = it->second;
      } else if (links.size() < LinkLimit) {
        links.push_back({*target, {}, text.nextLine()});
        byUrl.emplace(std::move(*target), links.size() - 1);
        index = links.size() - 1;
      } else {
        out.linksTruncated = true;
      }
      if (index && !text.capture) {
        anchorLink = *index;
        anchor.clear();
        text.startCapture(&anchor);
        exit.captures = true;
      }
      break;
    }
    default:
      break;
    }
    stack.push_back(exit);
    const bool pre = f.pre || preformatted(n), sectioned = f.sectioned || sectioning(n);
    for (auto *c = n->last_child; c; c = c->prev)
      stack.push_back({c, f.depth + 1, false, pre, sectioned});
  }
  if (text.capture && !anchor.empty()) {
    links[anchorLink].text = anchor;
    links[anchorLink].line = text.captureLine;
  }
  text.capture = nullptr;
  for (auto &link : links)
    out.links.push_back({{"url", std::move(link.url)},
                         {"text", std::move(link.text)},
                         {"line", std::clamp<std::size_t>(link.line, 1, text.lines)}});
}
} // namespace
HtmlDocument parseHtml(const std::string &body, const std::string &url, const std::string &contentType) {
  HtmlDocument out;
  const auto kind = classify(contentType, body);
  out.truncated = body.size() > InputLimit;
  const std::string_view input(body.data(), std::min(body.size(), InputLimit));
  const auto decoded = decode(input, chooseEncoding(input, contentType, kind == Kind::Html), out.truncated);
  Output text;
  if (kind == Kind::Text)
    text.text(decoded, true);
  else
    extractHtml(decoded, url, out, text);
  out.truncated = out.truncated || text.full;
  out.text = std::move(text.value);
  if (out.title.empty())
    out.title = url;
  return out;
}
} // namespace qws
