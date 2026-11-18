#pragma once

#include <QChar>
#include <QString>
#include <QUrl>
#include <lexbor/dom/interfaces/character_data.h>
#include <lexbor/dom/interfaces/element.h>
#include <lexbor/html/html.h>
#include <lexbor/html/parser.h>
#include <lexbor/html/tree.h>
#include <optional>
#include <qws/common.hpp>
#include <utility>
#include <vector>

namespace qws::html_dom {
inline constexpr std::size_t LinkUrlLimit = 4096;

using Node = lxb_dom_node_t;
using Document = std::unique_ptr<lxb_html_document_t, decltype(&lxb_html_document_destroy)>;

// Parser insertions and visited nodes. Reaching either budget truncates the page.
inline constexpr std::size_t NodeBudget = 100000;
// Open elements during parsing and traversal depth afterwards.
inline constexpr std::size_t DepthBudget = 512;
// Unclosed formatting elements the parser may clone into every new block.
inline constexpr std::size_t FormattingBudget = 1000;

[[noreturn]] inline void unsupported(const char *message) {
  throw Error("unsupported_content", message, 415);
}

inline void appendUtf8(std::string &out, char32_t cp) {
  if (cp < 0x80) {
    out.push_back(char(cp));
  } else if (cp < 0x800) {
    out.push_back(char(0xc0 | (cp >> 6)));
    out.push_back(char(0x80 | (cp & 0x3f)));
  } else if (cp < 0x10000) {
    out.push_back(char(0xe0 | (cp >> 12)));
    out.push_back(char(0x80 | ((cp >> 6) & 0x3f)));
    out.push_back(char(0x80 | (cp & 0x3f)));
  } else {
    out.push_back(char(0xf0 | (cp >> 18)));
    out.push_back(char(0x80 | ((cp >> 12) & 0x3f)));
    out.push_back(char(0x80 | ((cp >> 6) & 0x3f)));
    out.push_back(char(0x80 | (cp & 0x3f)));
  }
}
// Reads one scalar. Malformed sequences become U+FFFD and consume one byte.
inline char32_t nextScalar(std::string_view s, std::size_t &i) {
  const auto lead = static_cast<unsigned char>(s[i++]);
  if (lead < 0x80)
    return lead;
  int extra = 0;
  char32_t cp = 0, minimum = 0;
  if ((lead & 0xe0) == 0xc0) {
    extra = 1, cp = lead & 0x1f, minimum = 0x80;
  } else if ((lead & 0xf0) == 0xe0) {
    extra = 2, cp = lead & 0x0f, minimum = 0x800;
  } else if ((lead & 0xf8) == 0xf0) {
    extra = 3, cp = lead & 0x07, minimum = 0x10000;
  } else {
    return 0xfffd;
  }
  const auto start = i;
  for (int k = 0; k < extra; ++k) {
    if (i >= s.size() || (static_cast<unsigned char>(s[i]) & 0xc0) != 0x80) {
      i = start;
      return 0xfffd;
    }
    cp = (cp << 6) | (static_cast<unsigned char>(s[i++]) & 0x3f);
  }
  if (cp < minimum || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff)) {
    i = start;
    return 0xfffd;
  }
  return cp;
}
inline bool isSpace(char32_t cp) {
  return QChar::isSpace(cp);
}
// Controls and invisible formatting that can hide text from readers. U+FEFF,
// U+200C and U+200D remain because real scripts and emoji sequences use them.
inline bool dropped(char32_t cp) {
  if (cp < 0x80)
    return cp < 0x20 || cp == 0x7f;
  return cp == 0xad || cp == 0x200b || cp == 0x2060 || (cp >= 0x202a && cp <= 0x202e) ||
         (cp >= 0x2066 && cp <= 0x2069) || (cp >= 0xe0000 && cp <= 0xe007f) ||
         QChar::category(cp) == QChar::Other_Control;
}
inline char lowerAscii(char c) {
  return c >= 'A' && c <= 'Z' ? char(c - 'A' + 'a') : c;
}
inline bool equalsAscii(std::string_view value, std::string_view lower) {
  if (value.size() != lower.size())
    return false;
  for (std::size_t i = 0; i < value.size(); ++i)
    if (lowerAscii(value[i]) != lower[i])
      return false;
  return true;
}
inline bool startsWithAscii(std::string_view value, std::string_view lower) {
  return value.size() >= lower.size() && equalsAscii(value.substr(0, lower.size()), lower);
}
inline std::string_view trimAscii(std::string_view s) {
  constexpr std::string_view space = " \t\n\f\r";
  const auto first = s.find_first_not_of(space);
  if (first == std::string_view::npos)
    return {};
  return s.substr(first, s.find_last_not_of(space) - first + 1);
}
inline std::string_view view(const lxb_char_t *p, std::size_t n) {
  return p ? std::string_view(reinterpret_cast<const char *>(p), n) : std::string_view();
}
// Collapses whitespace and removes hidden characters from short inline text.
inline std::string inlineText(std::string_view s, std::size_t limit) {
  std::string out;
  std::size_t count = 0;
  bool space = false;
  for (std::size_t i = 0; i < s.size() && count < limit;) {
    const auto cp = nextScalar(s, i);
    if (isSpace(cp)) {
      space = !out.empty();
      continue;
    }
    if (dropped(cp))
      continue;
    if (space) {
      if (count + 1 >= limit)
        break;
      out.push_back(' ');
      ++count;
      space = false;
    }
    appendUtf8(out, cp);
    ++count;
  }
  return out;
}
inline std::string_view attribute(Node *n, std::string_view name) {
  size_t len = 0;
  const auto *p = lxb_dom_element_get_attribute(
      lxb_dom_interface_element(n), reinterpret_cast<const lxb_char_t *>(name.data()), name.size(), &len);
  return view(p, len);
}
inline bool hasAttribute(Node *n, std::string_view name) {
  return lxb_dom_element_has_attribute(lxb_dom_interface_element(n),
                                       reinterpret_cast<const lxb_char_t *>(name.data()), name.size());
}

// lexbor builds the whole tree before returning, and spec-conforming formatting
// reconstruction can clone every unclosed formatting element into each new
// block. Count insertions while parsing and stop at the first exhausted budget.
struct ParseGuard {
  const lxb_dom_document_mutation_cb_t *original = nullptr;
  std::size_t inserted = 0;
  bool exceeded = false;
};
inline thread_local ParseGuard *activeGuard = nullptr;
inline lxb_status_t guardInsertion(lxb_dom_node_t *node) {
  auto *guard = activeGuard;
  if (!guard)
    return LXB_STATUS_OK;
  auto *parser =
      node && node->owner_document ? static_cast<lxb_html_parser_t *>(node->owner_document->parser) : nullptr;
  auto *tree = parser ? parser->tree : nullptr;
  if (tree && (++guard->inserted > NodeBudget || tree->open_elements->length > DepthBudget ||
               tree->active_formatting->length > FormattingBudget)) {
    guard->exceeded = true;
    // The tokenizer stops before the next token. The current token can add at
    // most FormattingBudget reconstructed elements.
    if (tree->status == LXB_STATUS_OK)
      tree->status = LXB_STATUS_ABORTED;
  }
  return guard->original->inserted ? guard->original->inserted(node) : LXB_STATUS_OK;
}
// Returns false when a budget stopped the parse. The partial tree stays usable.
inline bool parseGuarded(lxb_html_document_t *doc, std::string_view input) {
  auto *dom = lxb_dom_interface_document(doc);
  ParseGuard guard{dom->mutation};
  // Keep lexbor's own element steps and add counting in front of insertion.
  auto mutations = *dom->mutation;
  mutations.inserted = guardInsertion;
  struct Restore {
    lxb_dom_document_t *dom;
    ParseGuard *previous;
    const lxb_dom_document_mutation_cb_t *original;
    ~Restore() {
      dom->mutation = original;
      activeGuard = previous;
    }
  } restore{dom, std::exchange(activeGuard, &guard), dom->mutation};
  dom->mutation = &mutations;
  const auto status =
      lxb_html_document_parse(doc, reinterpret_cast<const lxb_char_t *>(input.data()), input.size());
  if (guard.exceeded)
    return false;
  if (status != LXB_STATUS_OK)
    unsupported("HTML parsing failed.");
  return true;
}

inline std::optional<std::string> resolveHttp(const QUrl &base, std::string_view value) {
  auto href = QString::fromUtf8(value.data(), qsizetype(value.size()));
  // URL parsing ignores ASCII tab and newline anywhere and surrounding spaces.
  href.remove(QLatin1Char('\t'));
  href.remove(QLatin1Char('\n'));
  href.remove(QLatin1Char('\r'));
  href = href.trimmed();
  if (href.isEmpty())
    return std::nullopt;
  auto link = base.resolved(QUrl(href, QUrl::TolerantMode));
  link.setFragment(QString());
  if (!link.isValid() || link.host().isEmpty() || !link.userInfo().isEmpty() ||
      link.authority(QUrl::FullyEncoded).contains('@') ||
      (link.scheme() != "http" && link.scheme() != "https"))
    return std::nullopt;
  auto encoded = link.toString(QUrl::FullyEncoded).toStdString();
  if (encoded.size() > LinkUrlLimit)
    return std::nullopt;
  return encoded;
}
inline std::string textContent(Node *root, std::size_t byteLimit) {
  std::string out;
  std::vector<Node *> pending{root};
  for (std::size_t visited = 0; !pending.empty() && out.size() < byteLimit && visited < NodeBudget;
       ++visited) {
    auto *n = pending.back();
    pending.pop_back();
    if (n->type == LXB_DOM_NODE_TYPE_TEXT) {
      const auto &d = lxb_dom_interface_character_data(n)->data;
      auto s = view(d.data, d.length);
      if (s.size() > byteLimit - out.size()) {
        auto cut = byteLimit - out.size();
        while (cut > 0 && (static_cast<unsigned char>(s[cut]) & 0xc0) == 0x80)
          --cut;
        s = s.substr(0, cut);
      }
      out.append(s);
      continue;
    }
    if (n->type == LXB_DOM_NODE_TYPE_ELEMENT &&
        (n->local_name == LXB_TAG_SCRIPT || n->local_name == LXB_TAG_STYLE ||
         n->local_name == LXB_TAG_TEMPLATE))
      continue;
    for (auto *c = n->last_child; c; c = c->prev)
      pending.push_back(c);
  }
  return out;
}
} // namespace qws::html_dom
