#pragma once
#include <qws/service.hpp>
#include <unordered_set>
namespace qws {
struct ProtocolReply { int httpStatus=200; std::optional<Json> message; std::string sessionId; };
class Protocol {
public:
  using Now=std::function<Clock::time_point()>;
  explicit Protocol(Service &,std::string version="0.1.0",Now now={});
  ProtocolReply handle(const std::string &principal,const std::string &sessionId,const Json &message,
                       std::shared_ptr<CancelToken> cancellation={});
  ProtocolReply deleteSession(const std::string &principal,const std::string &sessionId);
  void shutdown();
  static std::optional<std::string> validateJsonRpc(const Json &);
private:
  struct Session {
    std::string principal;
    Clock::time_point expires;
    bool ready=false;
    std::size_t usedIdBytes=0;
    std::unordered_set<std::string> usedIds;
    std::unordered_map<std::string,std::shared_ptr<CancelToken>> inFlight;
  };
  static bool validId(const Json &);
  static std::string idKey(const Json &);
  static ProtocolReply rpcError(int,const Json &,int,const std::string &,const std::string &sid={});
  static Json toolResult(Json,bool);
  static void retireLocked(Session &);
  void expireLocked(Clock::time_point);
  ProtocolReply finishCall(const std::shared_ptr<Session> &,const std::string &,const std::string &,
                          const std::shared_ptr<CancelToken> &,ProtocolReply);
  Service &service_;
  std::string version_;
  Now now_;
  std::mutex mutex_;
  bool closing_=false;
  std::unordered_map<std::string,std::shared_ptr<Session>> sessions_;
};
}
