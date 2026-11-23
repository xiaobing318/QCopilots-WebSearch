"""Local cpp-httplib 0.66.0 patch: bounded input and owned-socket shutdown.

Copyright 2026 QCopilots-WebSearch contributors, Apache-2.0.
The modified httplib.h remains under its upstream MIT license.
"""
import hashlib

ORIGINAL_SHA256 = "51ddd1482c67acb31f3064e48329aade14acc7dd0fab31ee5397193697ea1c35"


def patch(raw):
    if hashlib.sha256(raw).hexdigest() != ORIGINAL_SHA256:
        raise RuntimeError("Unexpected cpp-httplib input. Refusing to patch.")
    text = raw.decode("utf-8")

    def replace(before, after, count=1):
        nonlocal text
        if text.count(before) != count:
            raise RuntimeError("cpp-httplib patch anchor mismatch: " + before[:70])
        text = text.replace(before, after)

    replace("process_and_close_socket", "process_socket", 5)
    replace("protected:\n  bool process_request(Stream &strm,", """protected:
  void drain_after_response(socket_t sock) noexcept;
  bool process_request(Stream &strm,""")
    replace("  virtual bool process_socket(socket_t sock);", """  virtual bool process_socket(socket_t sock);

  // The accept guard hands ownership to exactly one queued worker.
  void interrupt_accepted_sockets() noexcept;
  void close_accepted_socket(socket_t sock) noexcept;
  struct AcceptedSocketGuard {
    Server *server;
    socket_t sock;
    AcceptedSocketGuard(Server *s, socket_t fd) noexcept : server(s), sock(fd) {}
    AcceptedSocketGuard(const AcceptedSocketGuard &) = delete;
    AcceptedSocketGuard &operator=(const AcceptedSocketGuard &) = delete;
    ~AcceptedSocketGuard() {
      if (server) { server->close_accepted_socket(sock); }
    }
    void release() noexcept { server = nullptr; }
  };
  std::mutex accepted_mutex_;
  std::set<socket_t> accepted_sockets_;
  std::atomic<bool> accepted_stopping_{false};""")
    replace("inline void drain_and_close_socket(socket_t sock) noexcept {",
            "inline void drain_socket(socket_t sock) noexcept {")
    replace("""    total += static_cast<size_t>(n);
  }

  shutdown_socket(sock);
  close_socket(sock);
}""", """    total += static_cast<size_t>(n);
  }
}

inline void drain_and_close_socket(socket_t sock) noexcept {
  drain_socket(sock);
  shutdown_socket(sock);
  close_socket(sock);
}""")
    for kind in ("SocketStream", "SSLSocketStream"):
        replace(f"inline bool {kind}::wait_readable() const {{",
                f"""inline bool {kind}::wait_readable() const {{
  if (max_timeout_msec_ > 0 && duration() >= max_timeout_msec_) {{ return false; }}""")
        replace(f"inline ssize_t {kind}::read(char *ptr, size_t size) {{",
                f"""inline ssize_t {kind}::read(char *ptr, size_t size) {{
  if (max_timeout_msec_ > 0 && duration() >= max_timeout_msec_) {{
    error_ = Error::Timeout;
    return -1;
  }}""")
    replace("inline const char *SocketStream::buffered_data(size_t &size) const {", """inline const char *SocketStream::buffered_data(size_t &size) const {
  if (max_timeout_msec_ > 0 && duration() >= max_timeout_msec_) {
    size = 0;
    return nullptr;
  }""")
    replace("inline void Server::stop() noexcept {", """inline void Server::drain_after_response(socket_t sock) noexcept {
  if (!accepted_stopping_) { detail::drain_socket(sock); }
}

inline void Server::interrupt_accepted_sockets() noexcept {
  std::lock_guard<std::mutex> lock(accepted_mutex_);
  accepted_stopping_ = true;
  for (auto sock : accepted_sockets_) { detail::shutdown_socket(sock); }
}

inline void Server::close_accepted_socket(socket_t sock) noexcept {
  // Erase and close under one lock so stop() cannot touch a reused descriptor.
  std::lock_guard<std::mutex> lock(accepted_mutex_);
  accepted_sockets_.erase(sock);
  detail::shutdown_socket(sock);
  detail::close_socket(sock);
}

inline void Server::stop() noexcept {
  interrupt_accepted_sockets();""")
    replace("""  auto read_timeout_msec = read_timeout_sec_ * 1000 + read_timeout_usec_ / 1000;
""", """  // Both native and TLS requests use this path. shutdown() alone does not
  // wake a Windows read blocked in WSAPoll.
  strm.set_read_interrupt([this] { return accepted_stopping_.load(); });
  auto read_timeout_msec = read_timeout_sec_ * 1000 + read_timeout_usec_ / 1000;
""")
    replace("""  req.body_consumed_ = true;

  if (req.is_multipart_form_data()) {""", """  req.body_consumed_ = true;
  // Body, chunk framing and trailers are complete. Handlers have their own deadline.
  strm.set_max_timeout(0);

  if (req.is_multipart_form_data()) {""")
    replace("""  if (!is_valid()) { return -1; }

  svr_sock_ = create_server_socket""", """  if (!is_valid()) { return -1; }

  {
    std::lock_guard<std::mutex> lock(accepted_mutex_);
    if (is_running_ || !accepted_sockets_.empty() ||
        svr_sock_ != INVALID_SOCKET) {
      return -1;
    }
    accepted_stopping_ = false;
  }
  svr_sock_ = create_server_socket""")
    replace("""      detail::set_socket_opt_time(sock, SOL_SOCKET, SO_RCVTIMEO,
                                  read_timeout_sec_, read_timeout_usec_);
      detail::set_socket_opt_time(sock, SOL_SOCKET, SO_SNDTIMEO,
                                  write_timeout_sec_, write_timeout_usec_);

      if (tcp_nodelay_) { set_socket_opt(sock, IPPROTO_TCP, TCP_NODELAY, 1); }

      if (!task_queue->enqueue(
              [this, sock]() { process_socket(sock); })) {
        output_error_log(Error::ResourceExhaustion, nullptr);
        detail::shutdown_socket(sock);
        detail::close_socket(sock);
      }
    }

    task_queue->shutdown();""", """      AcceptedSocketGuard accepted(this, sock);
#ifndef CPPHTTPLIB_NO_EXCEPTIONS
      try {
#endif
        {
          std::lock_guard<std::mutex> lock(accepted_mutex_);
          if (accepted_stopping_) { break; }
          accepted_sockets_.insert(sock);
        }
        detail::set_socket_opt_time(sock, SOL_SOCKET, SO_RCVTIMEO,
                                    read_timeout_sec_, read_timeout_usec_);
        detail::set_socket_opt_time(sock, SOL_SOCKET, SO_SNDTIMEO,
                                    write_timeout_sec_, write_timeout_usec_);
        if (tcp_nodelay_) { set_socket_opt(sock, IPPROTO_TCP, TCP_NODELAY, 1); }
        if (task_queue->enqueue([this, sock]() {
              AcceptedSocketGuard owned(this, sock);
              if (accepted_stopping_) { return; }
              serve_guarded([&]() { return process_socket(sock); });
            })) {
          accepted.release();
        } else {
          serve_guarded([&]() {
            output_error_log(Error::ResourceExhaustion, nullptr);
            return false;
          });
        }
#ifndef CPPHTTPLIB_NO_EXCEPTIONS
      } catch (...) {
        ret = false;
        stop();
        break;
      }
#endif
    }

    // Includes unexpected accept-loop failure. Queued guards still close sockets.
    interrupt_accepted_sockets();
    task_queue->shutdown();""")
    replace("""      handler(req, res);
      return true;
    }
""", """      strm.set_max_timeout(0); // Includes routes with no request body.
      handler(req, res);
      return true;
    }
""")
    replace("""  // The read timeout bounds the request line and headers as a whole, so a
  // client sending them a byte at a time cannot hold the worker any longer.""",
            """  // One absolute input deadline covers headers, body, chunks and trailers.
  // Clear it only after input has been fully consumed.""")
    replace("""  strm.set_max_timeout(0);

  // RFC 9112 §6.3:""", """
  // RFC 9112 §6.3:""")
    replace("""      // Use WebSocket-specific read timeout instead of HTTP timeout
      strm.set_read_timeout""", """      // Use WebSocket-specific read timeout instead of HTTP timeout
      strm.set_max_timeout(0);
      strm.set_read_timeout""")
    replace("""    strm.set_max_timeout(read_timeout_msec);
    int dummy_status;""", """    // Retain the original input deadline while draining an unconsumed body.
    int dummy_status;""")
    replace("""  detail::drain_and_close_socket(sock);
  return ret;
}""", """  drain_after_response(sock);
  return ret;
}""")
    replace("""    last_ssl_error_ = static_cast<int>(get_error());
    detail::shutdown_socket(sock);
    detail::close_socket(sock);
    return false;""", """    last_ssl_error_ = static_cast<int>(get_error());
    return false; // The accepted-socket guard owns the descriptor.""")
    replace("""    free_session(session);
    detail::shutdown_socket(sock);
    detail::close_socket(sock);""", """    free_session(session);
    // A client may still be transmitting its TLS request body after an early
    // HTTP response. Half-close and discard in-flight ciphertext before the
    // accepted-socket guard closes the descriptor, as for plain HTTP.
    if (handshake_done && !websocket_upgraded) { drain_after_response(sock); }""")
    return text.encode("utf-8")
