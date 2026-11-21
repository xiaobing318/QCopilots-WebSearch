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
  Error(std::string value, std::string safeMessage, int httpStatus = 400, bool canRetry = false,
        int retryAfter = 0, std::string specificReason = {}, Json safeParameters = Json::object(),
        std::string diagnostic = {})
      : std::runtime_error(std::move(safeMessage)), code(std::move(value)), status(httpStatus),
        retryable(canRetry), retryAfterSeconds(retryAfter), reason(std::move(specificReason)),
        parameters(std::move(safeParameters)), localDiagnostic(std::move(diagnostic)) {}
  const std::string code;
  const int status;
  const bool retryable;
  const int retryAfterSeconds;
  const std::string reason;
  const Json parameters;
  // Deployment paths and OS diagnostics must never enter public errorPayload().
  const std::string localDiagnostic;

  Error withReason(std::string value, Json values = Json::object()) const {
    return Error(code, what(), status, retryable, retryAfterSeconds, std::move(value),
                 std::move(values), localDiagnostic);
  }
  Error withLocalDiagnostic(std::string value) const {
    return Error(code, what(), status, retryable, retryAfterSeconds, reason, parameters,
                 std::move(value));
  }
  Error withMessage(std::string value) const {
    return Error(code, std::move(value), status, retryable, retryAfterSeconds, reason,
                 parameters, localDiagnostic);
  }
};

struct CancelToken {
  std::atomic_bool cancelled{false};
  Clock::time_point deadline = Clock::now() + std::chrono::seconds(20);
  bool stopped() const {
    return cancelled.load(std::memory_order_relaxed) || Clock::now() >= deadline;
  }
};

inline Json errorPayload(const Error &error) {
  Json details = {{"code", error.code}, {"message", error.what()}, {"retryable", error.retryable}};
  const auto safeIdentifier = [](const std::string &value, bool clientId) {
    if (value.empty() || value.size() > 64)
      return false;
    for (const unsigned char character : value) {
      if (!(character >= 'a' && character <= 'z') && !(character >= '0' && character <= '9') &&
          character != '_' && !(clientId && ((character >= 'A' && character <= 'Z') ||
                                             character == '.' || character == '-')))
        return false;
    }
    return true;
  };
  if (safeIdentifier(error.reason, false)) {
    details["reason"] = error.reason;
    // Only the existing, non-secret client identifier is accepted as a dynamic
    // message parameter. Never forward arbitrary exception or upstream data.
    if (error.parameters.is_object() && error.parameters.contains("client_id") &&
        error.parameters.at("client_id").is_string()) {
      const auto value = error.parameters.at("client_id").get<std::string>();
      if (safeIdentifier(value, true))
        details["parameters"] = {{"client_id", value}};
    }
  }
  if (error.retryAfterSeconds > 0)
    details["retry_after_seconds"] = error.retryAfterSeconds;
  return {{"error", std::move(details)}};
}
} // namespace qws
