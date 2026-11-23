#pragma once
#include <qws/common.hpp>

inline void require(bool condition, const char *message) {
  if (!condition)
    throw std::runtime_error(message);
}

inline qws::Json mcpInitialize(int id, const char *version) {
  return {{"jsonrpc", "2.0"}, {"id", id}, {"method", "initialize"},
          {"params", {{"protocolVersion", version}, {"capabilities", qws::Json::object()},
                      {"clientInfo", {{"name", "http-test"}, {"version", "1"}}}}}};
}

inline qws::Json mcpToolCall(int id, const char *name, const qws::Json &arguments) {
  return {{"jsonrpc", "2.0"}, {"id", id}, {"method", "tools/call"},
          {"params", {{"name", name}, {"arguments", arguments}}}};
}

template<class Action>
void requireError(const char *code, const Action &action) {
  try {
    action();
  } catch (const qws::Error &error) {
    require(error.code == code, "Unexpected error code.");
    return;
  }
  throw std::runtime_error("Expected an error.");
}
