#pragma once
#include <httplib.h>
#include <qws/common.hpp>

namespace qws {
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
