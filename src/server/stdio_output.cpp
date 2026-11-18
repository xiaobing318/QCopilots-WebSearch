#include "stdio_output.hpp"
#include <algorithm>
#include <chrono>
#include <stdexcept>
#ifdef _WIN32
#define NOMINMAX
#include <windows.h>
#else
#include <cerrno>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <unistd.h>
#endif

namespace qws {
StdioOutput::StdioOutput() {
#ifndef _WIN32
  flags_ = fcntl(STDOUT_FILENO, F_GETFL);
  if (flags_ < 0 || fcntl(STDOUT_FILENO, F_SETFL, flags_ | O_NONBLOCK) < 0)
    throw std::runtime_error("Cannot configure stdio output");
#endif
  try {
    thread_ = std::thread([this] { run(); });
  } catch (...) {
#ifndef _WIN32
    fcntl(STDOUT_FILENO, F_SETFL, flags_);
#endif
    throw;
  }
}
StdioOutput::~StdioOutput() {
  finish();
}
void StdioOutput::fail() {
  failed_ = true;
  abort_ = true;
  changed_.notify_all();
}
bool StdioOutput::enqueue(std::string message) {
  std::lock_guard lock(mutex_);
  // Includes the active write. A stalled client cannot retain unbounded replies.
  if (closing_ || abort_ || message.size() > 1024 * 1024 || count_ >= 32 ||
      message.size() + 1 > 4 * 1024 * 1024 - bytes_) {
    failed_ = true;
    abort_ = true;
    changed_.notify_all();
    return false;
  }
  message += '\n';
  bytes_ += message.size();
  ++count_;
  queue_.push_back(std::move(message));
  changed_.notify_all();
  return true;
}
bool StdioOutput::write(const std::string &message) {
  size_t offset = 0;
  while (offset < message.size() && !abort_) {
#ifdef _WIN32
    DWORD written = 0;
    if (!WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), message.data() + offset,
                   DWORD(std::min<size_t>(16384, message.size() - offset)), &written, nullptr) ||
        !written)
      return false;
#else
    pollfd descriptor{STDOUT_FILENO, POLLOUT, 0};
    const int ready = poll(&descriptor, 1, 25);
    if (ready < 0 && errno == EINTR)
      continue;
    if (ready < 0 || (ready > 0 && (descriptor.revents & (POLLERR | POLLHUP | POLLNVAL))))
      return false;
    if (!ready)
      continue;
    const auto written =
        ::write(STDOUT_FILENO, message.data() + offset, std::min<size_t>(16384, message.size() - offset));
    if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
      continue;
    if (written <= 0)
      return false;
#endif
    offset += size_t(written);
  }
  return offset == message.size();
}
void StdioOutput::run() {
#ifndef _WIN32
  sigset_t signals;
  sigemptyset(&signals);
  sigaddset(&signals, SIGPIPE);
  pthread_sigmask(SIG_BLOCK, &signals, nullptr);
#endif
  for (;;) {
    std::string message;
    {
      std::unique_lock lock(mutex_);
      changed_.wait(lock, [this] { return abort_ || closing_ || !queue_.empty(); });
      if (abort_ || queue_.empty())
        break;
      message = std::move(queue_.front());
      queue_.pop_front();
    }
    const bool success = write(message);
    {
      std::lock_guard lock(mutex_);
      bytes_ -= message.size();
      --count_;
      if (!success) {
        if (!abort_)
          failed_ = true;
        abort_ = true;
      }
      changed_.notify_all();
    }
  }
  std::lock_guard lock(mutex_);
  exited_ = true;
  changed_.notify_all();
}
void StdioOutput::finish() {
  if (!thread_.joinable())
    return;
  {
    std::unique_lock lock(mutex_);
    closing_ = true;
    changed_.notify_all();
    // Preserve ordinary responses at EOF, but do not wait for a non-reading client.
    changed_.wait_for(lock, std::chrono::milliseconds(250), [this] { return exited_; });
    abort_ = true;
    changed_.notify_all();
  }
#ifdef _WIN32
  // Repeat cancellation until exit to cover the check-to-WriteFile race.
  while (WaitForSingleObject(thread_.native_handle(), 25) == WAIT_TIMEOUT)
    CancelSynchronousIo(thread_.native_handle());
#endif
  thread_.join();
#ifndef _WIN32
  fcntl(STDOUT_FILENO, F_SETFL, flags_);
#endif
}
} // namespace qws
