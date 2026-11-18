#pragma once

#include <qws/common.hpp>

namespace qws {
enum class SerpSource { DuckDuckGoHtml, DuckDuckGoLite, Bing };

struct SerpPage {
  Json results = Json::array();
  bool recognized = false;
  bool blocked = false;
};

SerpPage parseSerp(SerpSource source, std::string_view body, const std::string &url);
} // namespace qws
