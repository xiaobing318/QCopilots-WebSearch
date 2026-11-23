#include "stdio_output.hpp"
#include <QUuid>
#include <algorithm>
#include <cstdio>
#include <iostream>
#include <qws/server.hpp>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <cerrno>
#include <poll.h>
#include <unistd.h>
#endif

namespace qws {
int runStdio(Service &service) {
#ifdef _WIN32
  _setmode(_fileno(stdin), _O_BINARY);
  _setmode(_fileno(stdout), _O_BINARY);
#endif
  Protocol protocol(service, QCWS_VERSION, {}, Protocol::TransportPolicy::Stdio);
  std::string session;
  const auto principal = "stdio-" + QUuid::createUuid().toString(QUuid::WithoutBraces).toStdString();
  StdioOutput output;
  auto writeReply = [&](const ProtocolReply &reply, const Json *request = nullptr) {
    auto message = reply.message;
    // HTTP status codes have no channel on stdio. Notifications and cancelled
    // calls still have no response, but rejected requests receive an error.
    if (!message && reply.httpStatus >= 400 && request && request->is_object() &&
        request->contains("method") && (*request)["method"].is_string() && request->contains("id") &&
        Protocol::validId((*request)["id"])) {
      message =
          Json{{"jsonrpc", "2.0"},
               {"id", (*request)["id"]},
               {"error", {{"code", -32002}, {"message", "Session is not initialized or is unavailable."}}}};
    }
    if (message) {
      if (!output.enqueue(message->dump()))
        throw Error("output_unavailable", "Stdio output is unavailable.", 503);
    }
  };
  struct Task {
    std::thread thread;
    std::shared_ptr<std::atomic_bool> done;
  };
  constexpr size_t maxWorkers = 64;
  std::vector<Task> tasks;
  tasks.reserve(maxWorkers);
  const Protocol::ToolDispatcher dispatch = [&](Protocol::ToolTask execute) {
    if (tasks.size() >= maxWorkers)
      throw Error("busy", "Stdio worker capacity is exhausted.", 429, true)
          .withReason("business_capacity_exhausted");
    auto done = std::make_shared<std::atomic_bool>(false);
    tasks.push_back({std::thread([&, done, execute = std::move(execute)] {
                       try {
                         writeReply(execute());
                       } catch (...) {
                         output.fail();
                         protocol.shutdown();
                         service.shutdown();
                       }
                       *done = true;
                     }),
                     done});
  };
  std::string line;
  line.reserve(4096);
  std::string pending;
  size_t offset = 0;
  auto reloadAt = Clock::now() + std::chrono::seconds(2);
  const auto reloadSettings = [&] {
    if (Clock::now() >= reloadAt) {
      service.settings().reloadIfChanged();
      reloadAt = Clock::now() + std::chrono::seconds(2);
    }
  };
#ifdef _WIN32
  // A private high-resolution timer avoids Sleep's system-tick latency for
  // sequential MCP calls. Back off while idle without changing timer resolution
  // for other applications. Supported by both Windows deployment targets.
  std::unique_ptr<void, decltype(&CloseHandle)> inputTimer(
      CreateWaitableTimerExW(nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
                             TIMER_MODIFY_STATE | SYNCHRONIZE),
      &CloseHandle);
  if (!inputTimer)
    throw Error("input_unavailable", "Cannot configure stdio input.", 503);
#endif
  auto readInput = [&]() -> bool {
    char buffer[16384];
#ifdef _WIN32
    LONGLONG delay = 20000; // Relative timer units are 100 ns.
#endif
    for (;;) {
      reloadSettings();
      if (output.failed())
        throw Error("output_unavailable", "Stdio output is unavailable.", 503);
#ifdef _WIN32
      const auto input = GetStdHandle(STD_INPUT_HANDLE);
      DWORD available = sizeof(buffer);
      if (GetFileType(input) == FILE_TYPE_PIPE) {
        if (!PeekNamedPipe(input, nullptr, 0, nullptr, &available, nullptr)) {
          if (GetLastError() == ERROR_BROKEN_PIPE)
            return false;
          throw Error("input_unavailable", "Stdio input is unavailable.", 503);
        }
        if (!available) {
          LARGE_INTEGER due;
          due.QuadPart = -delay;
          if (!SetWaitableTimer(inputTimer.get(), &due, 0, nullptr, nullptr, FALSE) ||
              WaitForSingleObject(inputTimer.get(), 1000) != WAIT_OBJECT_0)
            throw Error("input_unavailable", "Stdio input wait failed.", 503);
          delay = std::min<LONGLONG>(delay * 2, 250000);
          continue;
        }
      }
      DWORD count = 0;
      if (!ReadFile(input, buffer, DWORD(std::min<size_t>(available, sizeof(buffer))), &count, nullptr)) {
        if (GetLastError() == ERROR_BROKEN_PIPE)
          return false;
        throw Error("input_unavailable", "Stdio input is unavailable.", 503);
      }
#else
      pollfd descriptor{STDIN_FILENO, POLLIN, 0};
      const int ready = poll(&descriptor, 1, 25);
      if ((ready < 0 && errno == EINTR) || ready == 0)
        continue;
      if (ready < 0 || (descriptor.revents & (POLLERR | POLLNVAL)))
        throw Error("input_unavailable", "Stdio input is unavailable.", 503);
      const auto count = ::read(STDIN_FILENO, buffer, sizeof(buffer));
      if (count < 0 && errno == EINTR)
        continue;
      if (count < 0)
        throw Error("input_unavailable", "Stdio input is unavailable.", 503);
#endif
      if (!count)
        return false;
      pending.assign(buffer, size_t(count));
      offset = 0;
      return true;
    }
  };
  // A line beyond the 1 MiB input budget is discarded through its newline
  // without buffering, so the process keeps serving later messages.
  enum class Input { Line, Overlong, End };
  auto readLine = [&]() -> Input {
    line.clear();
    bool overlong = false;
    for (;;) {
      if (offset == pending.size() && !readInput())
        return overlong ? Input::Overlong : line.empty() ? Input::End : Input::Line;
      const char ch = pending[offset++];
      if (ch == '\n')
        return overlong ? Input::Overlong : Input::Line;
      if (overlong)
        continue;
      if (line.size() >= 1024 * 1024) {
        overlong = true;
        line.clear();
        continue;
      }
      line += ch;
    }
  };
  int result = 0;
  try {
    while (!output.failed()) {
      reloadSettings();
      const auto input = readLine();
      if (input == Input::End)
        break;
      std::erase_if(tasks, [](Task &t) {
        if (t.done->load()) {
          t.thread.join();
          return true;
        }
        return false;
      });
      if (input == Input::Overlong) {
        writeReply({200,
                    Json{{"jsonrpc", "2.0"},
                         {"id", nullptr},
                         {"error", {{"code", -32600}, {"message", "Message exceeds 1 MiB"}}}},
                    session});
        continue;
      }
      // Blank or CR-only lines are framing noise, not messages.
      if (line.find_first_not_of(" \t\r") == std::string::npos)
        continue;
      auto message = parseBoundedJson(line);
      if (message.is_discarded()) {
        writeReply({200,
                    Json{{"jsonrpc", "2.0"},
                         {"id", nullptr},
                         {"error", {{"code", -32700}, {"message", "Parse error"}}}},
                    session});
        continue;
      }
      auto reply = protocol.handle(principal, session, message, {}, dispatch);
      if (!reply.sessionId.empty())
        session = reply.sessionId;
      writeReply(reply, &message);
    }
  } catch (const std::exception &) {
    std::cerr << "Stdio input failed.\n";
    result = 1;
  }
  protocol.shutdown();
  service.shutdown();
  for (auto &t : tasks)
    if (t.thread.joinable())
      t.thread.join();
  output.finish();
  return result || output.failed() ? 1 : 0;
}
} // namespace qws
