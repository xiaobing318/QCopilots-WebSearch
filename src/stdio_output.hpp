#pragma once
#include <atomic>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <string>
#include <thread>

namespace qws {
// Only the writer touches stdout. Producers never wait for a pipe reader, and
// EOF can cancel a blocked write before joining request workers.
class StdioOutput {
public:
  StdioOutput();
  ~StdioOutput();
  bool enqueue(std::string message);
  bool failed() const {
    return failed_.load();
  }
  void fail();
  void finish();

private:
  void run();
  bool write(const std::string &message);
  std::mutex mutex_;
  std::condition_variable changed_;
  std::deque<std::string> queue_;
  size_t bytes_ = 0, count_ = 0;
  bool closing_ = false, exited_ = false;
  std::atomic_bool abort_{false}, failed_{false};
#ifndef _WIN32
  int flags_ = -1;
#endif
  std::thread thread_;
};
} // namespace qws
