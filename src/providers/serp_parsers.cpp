#include "serp_parsers.hpp"
#include "../core/html_dom.hpp"
#include <QByteArray>
#include <QUrlQuery>
#include <algorithm>

namespace qws {
namespace {
using namespace html_dom;

constexpr std::size_t SerpInputLimit = 1024 * 1024;
constexpr std::size_t CandidateLimit = 100;
constexpr std::size_t CardBudget = 4096;

bool isTag(const Node *node, lxb_tag_id_t tag) {
  return node->type == LXB_DOM_NODE_TYPE_ELEMENT && node->local_name == tag;
}

bool hasClass(Node *node, std::string_view name) {
  constexpr std::string_view spaces = " \t\r\n\f";
  auto classes = trimAscii(attribute(node, "class"));
  while (!classes.empty()) {
    const auto end = classes.find_first_of(spaces);
    if (classes.substr(0, end) == name)
      return true;
    if (end == std::string_view::npos)
      break;
    classes = trimAscii(classes.substr(end));
  }
  return false;
}

bool inert(const Node *node) {
  return isTag(node, LXB_TAG_SCRIPT) || isTag(node, LXB_TAG_STYLE) ||
         isTag(node, LXB_TAG_NOSCRIPT) || isTag(node, LXB_TAG_TEMPLATE);
}

template<class Visitor>
Node *findElement(Node *root, std::size_t budget, Visitor visit) {
  if (!root)
    return nullptr;
  auto *node = root;
  for (std::size_t visited = 0; visited < budget; ++visited) {
    if (node->type == LXB_DOM_NODE_TYPE_ELEMENT && visit(node))
      return node;
    if (node->first_child && !inert(node)) {
      node = node->first_child;
      continue;
    }
    while (node != root && !node->next)
      node = node->parent;
    if (node == root)
      break;
    node = node->next;
  }
  return nullptr;
}

std::string text(Node *node, std::size_t limit) {
  return node ? inlineText(textContent(node, limit * 4), limit) : std::string{};
}

bool sameSite(const QString &host, const QString &site) {
  return host == site || host.endsWith('.' + site);
}

// Strata (MIT) documented the DDG uddg and Bing a1 link formats.
// Copyright (c) 2026 Niko1221 and the Strata contributors.
std::optional<std::string> resultUrl(const QUrl &base, std::string_view href, bool bing) {
  const auto link = resolveHttp(base, href);
  if (!link)
    return std::nullopt;
  const QUrl url(QString::fromStdString(*link), QUrl::StrictMode);
  if (!sameSite(url.host().toLower(), bing ? QStringLiteral("bing.com") : QStringLiteral("duckduckgo.com")))
    return link;

  const QUrlQuery query(url);
  if (bing) {
    if (url.path() != "/ck/a")
      return std::nullopt;
    const auto items = query.allQueryItemValues("u", QUrl::FullyDecoded);
    if (items.size() != 1 || !items.front().startsWith("a1"))
      return std::nullopt;
    const auto decoded = QByteArray::fromBase64Encoding(
        items.front().mid(2).toLatin1(), QByteArray::Base64UrlEncoding | QByteArray::AbortOnBase64DecodingErrors);
    if (!decoded)
      return std::nullopt;
    return resolveHttp(QUrl(), {decoded.decoded.constData(), std::size_t(decoded.decoded.size())});
  }

  if (url.path() != "/l/" && url.path() != "/l")
    return std::nullopt;
  const auto items = query.allQueryItemValues("uddg", QUrl::FullyDecoded);
  if (items.size() != 1)
    return std::nullopt;
  return resolveHttp(QUrl(), items.front().toStdString());
}

void addResult(SerpPage &page, const QUrl &base, Node *anchor, Node *snippet, Node *date, bool bing) {
  const auto link = resultUrl(base, attribute(anchor, "href"), bing);
  auto title = text(anchor, 512);
  if (!link || title.empty())
    return;
  Json row = {{"url", *link},
              {"title", std::move(title)},
              {"content", text(snippet, 2048)},
              {"engines", Json::array({bing ? "bing" : "duckduckgo"})}};
  if (date) {
    const auto when = inlineText(attribute(date, "datetime"), 64);
    if (!when.empty())
      row["publishedDate"] = when;
  }
  page.results.push_back(std::move(row));
}

bool ad(Node *node) {
  return hasClass(node, "result--ad") || hasClass(node, "result__ad") ||
         hasClass(node, "result-sponsored") || attribute(node, "data-result") == "ad";
}

Node *ancestor(Node *node, lxb_tag_id_t tag) {
  for (auto *parent = node->parent; parent; parent = parent->parent)
    if (isTag(parent, tag))
      return parent;
  return nullptr;
}

void duckDuckGoHtml(SerpPage &page, const QUrl &base, Node *anchor) {
  auto *card = anchor->parent;
  while (card && card->type == LXB_DOM_NODE_TYPE_ELEMENT) {
    if (ad(card))
      return;
    if (hasClass(card, "result") || hasClass(card, "web-result"))
      break;
    card = card->parent;
  }
  // A recognized result link remains usable on a page without a result wrapper.
  if (!card || card->type != LXB_DOM_NODE_TYPE_ELEMENT)
    card = anchor->parent;
  auto *snippet = findElement(card, CardBudget, [](auto *node) { return hasClass(node, "result__snippet"); });
  auto *date = findElement(card, CardBudget, [](auto *node) { return isTag(node, LXB_TAG_TIME); });
  addResult(page, base, anchor, snippet, date, false);
}

void duckDuckGoLite(SerpPage &page, const QUrl &base, Node *anchor) {
  auto *row = ancestor(anchor, LXB_TAG_TR);
  Node *snippet = nullptr;
  for (int index = 0; row && index < 8; ++index, row = row->next) {
    if (!isTag(row, LXB_TAG_TR))
      continue;
    if (index > 0 && findElement(row, CardBudget, [](auto *node) { return hasClass(node, "result-link"); }))
      break;
    snippet = findElement(row, CardBudget, [](auto *node) { return hasClass(node, "result-snippet"); });
    if (snippet)
      break;
  }
  addResult(page, base, anchor, snippet, nullptr, false);
}

void bingResult(SerpPage &page, const QUrl &base, Node *card) {
  auto *heading = findElement(card, CardBudget, [](auto *node) { return isTag(node, LXB_TAG_H2); });
  auto *anchor = findElement(heading, CardBudget, [](auto *node) { return isTag(node, LXB_TAG_A); });
  if (!anchor)
    return;
  auto *snippet = findElement(card, CardBudget, [](auto *node) {
    return isTag(node, LXB_TAG_P) && (hasClass(node, "b_lineclamp2") || hasClass(node, "b_lineclamp3") ||
                                     hasClass(node, "b_lineclamp4"));
  });
  if (!snippet) {
    auto *caption = findElement(card, CardBudget, [](auto *node) { return hasClass(node, "b_caption"); });
    snippet = findElement(caption, CardBudget, [](auto *node) { return isTag(node, LXB_TAG_P); });
  }
  auto *date = findElement(card, CardBudget, [](auto *node) { return isTag(node, LXB_TAG_TIME); });
  addResult(page, base, anchor, snippet, date, true);
}

bool blocked(Node *node) {
  const auto id = attribute(node, "id");
  if (id == "challenge-form" || id == "b_captcha" || id == "b_captchaContainer" ||
      id == "captcha" || id == "cf-challenge-running" ||
      hasClass(node, "anomaly-modal") || hasClass(node, "anomaly-modal__modal") ||
      hasClass(node, "b_captcha"))
    return true;
  if (isTag(node, LXB_TAG_FORM)) {
    const auto action = attribute(node, "action");
    if (action.find("/anomaly.js") != std::string_view::npos)
      return true;
  }
  if (isTag(node, LXB_TAG_TITLE) || isTag(node, LXB_TAG_H1)) {
    const auto label = QString::fromStdString(text(node, 160)).toLower();
    return label.contains("unusual traffic") || label.contains("verify you are human") ||
           label.contains("are you a robot") || label.contains("verify you are a human") ||
           label == "captcha";
  }
  return false;
}

bool emptyResults(Node *node, bool bing) {
  const auto id = attribute(node, "id");
  if (bing)
    return id == "b_no" || hasClass(node, "b_no") || hasClass(node, "b_noResults");
  return id == "no-results" || hasClass(node, "no-results") || hasClass(node, "result--no-result") ||
         hasClass(node, "result--no-results");
}
} // namespace

SerpPage parseSerp(SerpSource source, std::string_view body, const std::string &url) {
  SerpPage page;
  if (body.empty() || body.size() > SerpInputLimit)
    return page;

  Document document(lxb_html_document_create(), lxb_html_document_destroy);
  if (!document)
    throw Error("internal_error", "Cannot allocate search result HTML parser.", 500);
  lxb_html_document_scripting_set(document.get(), true);
  try {
    if (!parseGuarded(document.get(), body))
      return page;
  } catch (const Error &error) {
    if (error.code != "unsupported_content")
      throw;
    return page;
  }
  const QUrl base(QString::fromStdString(url), QUrl::StrictMode);
  const bool bing = source == SerpSource::Bing;
  std::size_t candidates = 0;
  findElement(lxb_dom_interface_node(document.get()), NodeBudget, [&](Node *node) {
    page.blocked = page.blocked || blocked(node);
    page.recognized = page.recognized || emptyResults(node, bing);

    if (bing && isTag(node, LXB_TAG_LI) && hasClass(node, "b_algo")) {
      page.recognized = true;
      bingResult(page, base, node);
      ++candidates;
    } else if (isTag(node, LXB_TAG_A) && source == SerpSource::DuckDuckGoHtml && hasClass(node, "result__a")) {
      page.recognized = true;
      duckDuckGoHtml(page, base, node);
      ++candidates;
    } else if (isTag(node, LXB_TAG_A) && source == SerpSource::DuckDuckGoLite && hasClass(node, "result-link")) {
      page.recognized = true;
      duckDuckGoLite(page, base, node);
      ++candidates;
    }
    return candidates >= CandidateLimit;
  });
  // A results page can contain search results about CAPTCHAs. Prefer parsed results.
  if (!page.results.empty())
    page.blocked = false;
  return page;
}
} // namespace qws
