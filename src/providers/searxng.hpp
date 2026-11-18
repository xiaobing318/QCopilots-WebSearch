#pragma once
#include <qws/providers.hpp>
namespace qws {
std::unique_ptr<SearchProvider> makeSearxng(const ProviderConfig &, const HttpClient &,
                                           const BackendSearchTransport &);
}
