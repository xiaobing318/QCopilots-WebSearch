#pragma once
#include <algorithm>
#include <array>
#include <deque>
#include <functional>
#include <qws/service.hpp>
#include <string_view>
#include <unordered_set>
namespace qws {
struct ProtocolReply {
  int httpStatus = 200;
  std::optional<Json> message;
  std::string sessionId;
};
class Protocol {
public:
  using Now = std::function<Clock::time_point()>;
  using ToolTask = std::function<ProtocolReply()>;
  using ToolDispatcher = std::function<void(ToolTask)>;
  enum class TransportPolicy { StreamableHttp, Stdio };
  // Revisions supported on both transports, newest first. The 2024 revision
  // is also supported on stdio. Its HTTP transport predates Streamable HTTP.
  static constexpr std::array<std::string_view, 2> SupportedVersions{"2025-11-25", "2025-06-18"};
  static constexpr std::string_view LegacyStdioVersion = "2024-11-05";
  static bool supportsVersion(std::string_view version,
                              TransportPolicy transport = TransportPolicy::StreamableHttp) {
    return std::ranges::find(SupportedVersions, version) != SupportedVersions.end() ||
           (transport == TransportPolicy::Stdio && version == LegacyStdioVersion);
  }
  explicit Protocol(Service &, std::string version = QCWS_VERSION, Now now = {},
                    TransportPolicy transport = TransportPolicy::StreamableHttp);
  // A dispatcher receives already-registered tool calls. Run each task once
  // before destroying the protocol. A queued call returns 202 immediately.
  ProtocolReply handle(const std::string &principal, const std::string &sessionId, const Json &message,
                       std::shared_ptr<CancelToken> cancellation = {}, const ToolDispatcher &dispatch = {});
  int sessionStatus(const std::string &principal, const std::string &sessionId) const;
  ProtocolReply deleteSession(const std::string &principal, const std::string &sessionId);
  void shutdown();
  static std::optional<std::string> validateJsonRpc(const Json &);
  static bool validId(const Json &);

private:
  ProtocolReply handleRequest(const std::string &, const std::string &, const Json &,
                              std::shared_ptr<CancelToken>, const ToolDispatcher &);
  ProtocolReply boundedReply(ProtocolReply, const std::string &initializingPrincipal = {});
  struct Session {
    std::string principal;
    std::string negotiatedVersion;
    Clock::time_point expires;
    bool ready = false;
    std::size_t usedIdBytes = 0;
    std::unordered_set<std::string> usedIds;
    std::deque<std::string> recentIds;
    std::unordered_map<std::string, std::shared_ptr<CancelToken>> inFlight;
    bool idleExpired(Clock::time_point now) const {
      return expires <= now && inFlight.empty();
    }
  };
  static std::string idKey(const Json &);
  static ProtocolReply rpcError(int, const Json &, int, const std::string &, const std::string &sid = {});
  static Json toolResult(Json, bool, std::string_view version);
  static void retireLocked(Session &);
  void expireLocked(Clock::time_point);
  void rememberIdLocked(Session &, const std::string &);
  ProtocolReply finishCall(const std::shared_ptr<Session> &, const std::string &, const std::string &,
                           const std::shared_ptr<CancelToken> &, ProtocolReply);
  Service &service_;
  std::string version_;
  Now now_;
  TransportPolicy transport_;
  mutable std::mutex mutex_;
  bool closing_ = false;
  std::unordered_map<std::string, std::shared_ptr<Session>> sessions_;
};
} // namespace qws
