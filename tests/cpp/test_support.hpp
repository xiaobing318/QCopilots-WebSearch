#pragma once
#include <qws/common.hpp>

inline void require(bool condition, const char *message) {
  if (!condition)
    throw std::runtime_error(message);
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
