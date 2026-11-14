#pragma once
#include <qws/common.hpp>
namespace qws {
struct HtmlDocument {std::string title,text;Json links=Json::array();bool truncated=false;};
HtmlDocument parseHtml(const std::string &body,const std::string &url,const std::string &contentType);
}
