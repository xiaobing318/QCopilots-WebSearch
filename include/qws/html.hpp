#pragma once
#include <qws/common.hpp>
namespace qws {
// Readable page text in light Markdown. Each link is {"url", "text", "line"}:
// anchor text is simplified to at most 200 Unicode characters (possibly empty)
// and line is the 1-based text line where the anchor text starts.
struct HtmlDocument {
  std::string title, text;
  Json links = Json::array();
  // Input, text or HTML complexity budgets cut the page.
  bool truncated = false;
  // Only the link-count budget was reached.
  bool linksTruncated = false;
};
HtmlDocument parseHtml(const std::string &body, const std::string &url, const std::string &contentType);
} // namespace qws
