#pragma once
#include <functional>
#include <httplib.h>
#include <qws/common.hpp>

namespace qws {
inline void runListener(httplib::Server &listener, const std::function<void()> &onExit) {
  bool graceful = false;
  try {
    graceful = listener.listen_after_bind();
  } catch (...) {
    // Allocation or thread creation can fail before the accept loop starts.
  }
  if (!graceful)
    listener.decommission(); // Release a concurrent wait_until_ready() on startup failure.
  if (onExit)
    onExit();
}

inline void listenerSocketOptions(socket_t socket) {
  // Replace httplib's reuse defaults. Windows SO_REUSEADDR and Unix
  // SO_REUSEPORT can let independent servers claim the same listener.
#ifdef _WIN32
  const bool configured = httplib::set_socket_opt(socket, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, 1);
#else
  // Reuse a closed listener's address for restart, but never enable REUSEPORT.
  const bool configured = httplib::set_socket_opt(socket, SOL_SOCKET, SO_REUSEADDR, 1);
#endif
  if (!configured) {
#ifdef _WIN32
    ::closesocket(socket);
#else
    ::close(socket);
#endif
    throw Error("invalid_configuration", "Cannot configure exclusive listener ownership.");
  }
}
} // namespace qws
