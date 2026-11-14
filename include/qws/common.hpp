#pragma once

#include <atomic>
#include <chrono>
#include <memory>
#include <nlohmann/json.hpp>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>

namespace qws {
using Json = nlohmann::json;
using Clock = std::chrono::steady_clock;

// Bound nesting before constructing a DOM. nlohmann's parser is iterative, but
// copying a deeply nested value is recursive and can exhaust a worker's stack.
inline Json parseBoundedJson(std::string_view input) {
  unsigned depth = 0;
  bool quoted = false, escaped = false;
  for (const char ch : input) {
    if (quoted) {
      if (escaped)
        escaped = false;
      else if (ch == '\\')
        escaped = true;
      else if (ch == '"')
        quoted = false;
    } else if (ch == '"') {
      quoted = true;
    } else if (ch == '{' || ch == '[') {
      if (++depth > 64)
        return Json(Json::value_t::discarded);
    } else if (ch == '}' || ch == ']') {
      if (depth == 0)
        return Json(Json::value_t::discarded);
      --depth;
    }
  }
  return Json::parse(input.begin(), input.end(), nullptr, false);
}

class Error final : public std::runtime_error {
public:
  Error(std::string value, std::string safeMessage, int httpStatus = 400, bool canRetry = false)
      : std::runtime_error(std::move(safeMessage)), code(std::move(value)), status(httpStatus),
        retryable(canRetry) {}
  const std::string code;
  const int status;
  const bool retryable;
};

struct CancelToken {
  std::atomic_bool cancelled{false};
  Clock::time_point deadline = Clock::now() + std::chrono::seconds(20);
  bool stopped() const {
    return cancelled.load(std::memory_order_relaxed) || Clock::now() >= deadline;
  }
};

inline Json errorPayload(const Error &error) {
  return {{"error", {{"code", error.code}, {"message", error.what()}, {"retryable", error.retryable}}}};
}
} // namespace qws
