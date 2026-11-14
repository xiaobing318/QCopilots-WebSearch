#include <qws/server.hpp>
#include <httplib.h>
#include <QStringList>
#include <QUuid>
#include <iostream>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif
namespace qws {
namespace {
void sendJson(httplib::Response &response,const Json &payload,int status=200) {
  auto encoded=payload.dump();
  if(encoded.size()>1024*1024) {
    response.status=500;response.set_content(errorPayload(Error("internal_error","Response exceeded its wire budget.",500)).dump(),"application/json");return;
  }
  response.status=status;response.set_content(encoded,"application/json");
  if(status==429) response.set_header("Retry-After","1");
  if(status==401) response.set_header("WWW-Authenticate","Bearer");
}
void sendError(httplib::Response &response,const Error &error){sendJson(response,errorPayload(error),error.status);}
bool contains(const Json &array,const std::string &value,bool insensitive=false) {
  return std::any_of(array.begin(),array.end(),[&](const Json &v){return insensitive?
    QString::fromStdString(v.get<std::string>()).compare(QString::fromStdString(value),Qt::CaseInsensitive)==0:v==value;});
}
bool accepts(const std::string &header,const char *mime) {
  for(const auto &part:QString::fromStdString(header).split(',')) {
    if(part.section(';',0,0).trimmed().compare(QString::fromLatin1(mime),Qt::CaseInsensitive)!=0) continue;
    bool enabled=true;
    for(const auto &parameter:part.split(';').mid(1)) if(parameter.section('=',0,0).trimmed()=="q") {
      bool valid=false;const auto q=parameter.section('=',1).trimmed().toDouble(&valid);if(!valid||q<=0) enabled=false;
    }
    if(enabled) return true;
  }
  return false;
}
struct DisconnectMonitor {
  std::jthread thread;
  DisconnectMonitor(const httplib::Request &request,const std::shared_ptr<CancelToken> &token)
    :thread([&request,token](std::stop_token stop){while(!stop.stop_requested()) {
      if(request.is_connection_closed()){token->cancelled=true;return;}
      std::this_thread::sleep_for(std::chrono::milliseconds(25));
    }}) {}
};
Json parse(const std::string &body) {
  auto j=Json::parse(body,nullptr,false);
  if(j.is_discarded()) throw Error("invalid_argument","Request must contain valid UTF-8 JSON.");
  return j;
}
}
struct HttpServer::Impl {
  Service &service;Config config;Protocol protocol;
  std::vector<ClientIdentity> clients;
  httplib::Server business,control;
  std::thread businessThread,controlThread;
  int bp=0,cp=0;
  bool started=false;
  Impl(Service &s,Config c):service(s),config(std::move(c)),protocol(s),clients(config.clients()) {
    setup(business,config.value["business"],false);setup(control,config.value["control"],true);
  }
  std::string authorize(const httplib::Request &request) const {
    if(request.get_header_value_count("Authorization")!=1) throw Error("unauthorized","Exactly one bearer credential is required.",401);
    return authenticate(clients,request.get_header_value("Authorization"));
  }
  void setup(httplib::Server &server,Json listener,bool controls) {
    const auto threads=controls?4:config.value["limits"]["max_concurrent"].get<int>()+4;
    server.new_task_queue=[threads]{return new httplib::ThreadPool(threads,threads,16);};
    server.set_read_timeout(2).set_write_timeout(2).set_keep_alive_max_count(4).set_keep_alive_timeout(1)
      .set_payload_max_length(1024*1024).set_compress(false);
    server.set_default_headers({{"Cache-Control","no-store"},{"X-Content-Type-Options","nosniff"}});
    server.set_pre_routing_handler([this,listener](const httplib::Request &req,httplib::Response &res) {
      try {
        if(req.get_header_value_count("Host")!=1||!contains(listener["allowed_hosts"],req.get_header_value("Host"),true))
          throw Error("forbidden","Host is not allowed.",403);
        if(req.get_header_value_count("Origin")>1) throw Error("forbidden","Origin is not allowed.",403);
        if(req.has_header("Origin")) {
          const auto origin=req.get_header_value("Origin");
          if(!contains(listener["origins"],origin)) throw Error("forbidden","Origin is not allowed.",403);
          res.set_header("Access-Control-Allow-Origin",origin);res.set_header("Vary","Origin");
          res.set_header("Access-Control-Expose-Headers","Mcp-Session-Id");
        }
        if(req.method=="OPTIONS") {
          if(!req.has_header("Origin")) throw Error("forbidden","Browser preflight requires an allowed Origin.",403);
          res.status=204;res.set_header("Access-Control-Allow-Methods","GET, POST, DELETE, OPTIONS");
          res.set_header("Access-Control-Allow-Headers","Authorization, Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version");
          res.set_header("Access-Control-Max-Age","600");return httplib::Server::HandlerResponse::Handled;
        }
        (void)authorize(req);
        return httplib::Server::HandlerResponse::Unhandled;
      } catch(const Error &e){sendError(res,e);return httplib::Server::HandlerResponse::Handled;}
    });
    server.set_error_handler([](const httplib::Request &,httplib::Response &res) {
      if(!res.body.empty()) return;
      if(res.status==413) sendError(res,Error("request_too_large","Request body exceeds 1 MiB.",413));
      else sendError(res,Error("http_error","HTTP request could not be handled.",res.status));
    });
    server.set_exception_handler([](const httplib::Request &,httplib::Response &res,std::exception_ptr){sendError(res,Error("internal_error","Internal server error.",500));});
    if(controls) {
      server.Get("/health/live",[](const auto &,auto &res){sendJson(res,{{"contract_version",1},{"live",true}});});
      server.Get("/health/ready",[this](const auto &,auto &res){auto s=service.status();sendJson(res,s,s["service"]=="ready"?200:503);});
      server.Get("/metrics",[this](const auto &,auto &res){const auto s=service.status();res.set_content(
        "# TYPE qcws_active_calls gauge\nqcws_active_calls "+s["active_calls"].dump()+"\n# TYPE qcws_active_upstream gauge\nqcws_active_upstream "+s["active_upstream"].dump()+"\n","text/plain; version=0.0.4");});
      return;
    }
    server.Get("/v1/status",[this](const auto &,auto &res){sendJson(res,service.status());});
    for(const auto &name:{"search","open","find"}) server.Post(std::string("/v1/")+name,[this,name](const auto &req,auto &res) {
      try {
        if(req.get_header_value_count("Content-Type")!=1||!accepts(req.get_header_value("Content-Type"),"application/json"))
          throw Error("invalid_argument","Content-Type application/json is required.",415);
        auto token=std::make_shared<CancelToken>();token->deadline=Clock::now()+std::chrono::seconds(60);
        DisconnectMonitor monitor(req,token);
        sendJson(res,service.call(std::string("web_")+name,parse(req.body),authorize(req),token));
      } catch(const Error &e){sendError(res,e);}
    });
    server.Get("/mcp",[](const auto &,auto &res){res.status=405;res.set_header("Allow","POST, DELETE");});
    server.Delete("/mcp",[this](const auto &req,auto &res){
      const auto reply=protocol.deleteSession(authorize(req),req.get_header_value("Mcp-Session-Id"));res.status=reply.httpStatus;
    });
    server.Post("/mcp",[this](const auto &req,auto &res){
      if(req.get_header_value_count("Content-Type")!=1||!accepts(req.get_header_value("Content-Type"),"application/json")) {
        sendError(res,Error("invalid_argument","Content-Type application/json is required.",415));return;
      }
      if(!accepts(req.get_header_value("Accept"),"application/json")||!accepts(req.get_header_value("Accept"),"text/event-stream")) {
        sendError(res,Error("invalid_argument","Accept must include application/json and text/event-stream.",406));return;
      }
      if(req.get_header_value_count("Mcp-Session-Id")>1||req.get_header_value_count("MCP-Protocol-Version")>1||
         (req.has_header("MCP-Protocol-Version")&&req.get_header_value("MCP-Protocol-Version")!="2025-06-18")) {
        sendError(res,Error("invalid_argument","Unsupported protocol header."));return;
      }
      auto message=Json::parse(req.body,nullptr,false);
      if(message.is_discarded()){sendJson(res,{{"jsonrpc","2.0"},{"id",nullptr},{"error",{{"code",-32700},{"message","Parse error"}}}},400);return;}
      auto token=std::make_shared<CancelToken>();token->deadline=Clock::now()+std::chrono::seconds(60);
      DisconnectMonitor monitor(req,token);
      const auto reply=protocol.handle(authorize(req),req.get_header_value("Mcp-Session-Id"),message,token);
      if(!reply.sessionId.empty()) res.set_header("Mcp-Session-Id",reply.sessionId);
      if(reply.message) sendJson(res,*reply.message,reply.httpStatus);else res.status=reply.httpStatus;
    });
  }
  void start() {
    auto bind=[](httplib::Server &s,const Json &c){const int p=c["port"];const std::string host=c["host"];
      if(p==0) return s.bind_to_any_port(host);return s.bind_to_port(host,p)?p:-1;};
    bp=bind(business,config.value["business"]);if(bp<0) throw Error("invalid_configuration","Cannot bind business listener.");
    cp=bind(control,config.value["control"]);if(cp<0){business.stop();throw Error("invalid_configuration","Cannot bind control listener.");}
    businessThread=std::thread([this]{business.listen_after_bind();});
    controlThread=std::thread([this]{control.listen_after_bind();});started=true;
    business.wait_until_ready();control.wait_until_ready();
  }
  void stop() {
    if(!started) return;
    protocol.shutdown();service.shutdown();business.stop();control.stop();
    if(businessThread.joinable()) businessThread.join();if(controlThread.joinable()) controlThread.join();started=false;
  }
};
HttpServer::HttpServer(Service &s,Config c):d_(std::make_unique<Impl>(s,std::move(c))){}
HttpServer::~HttpServer(){stop();}
void HttpServer::start(){d_->start();}void HttpServer::stop(){d_->stop();}
int HttpServer::businessPort()const{return d_->bp;}int HttpServer::controlPort()const{return d_->cp;}
int runStdio(Service &service) {
#ifdef _WIN32
  _setmode(_fileno(stdin),_O_BINARY);_setmode(_fileno(stdout),_O_BINARY);
#endif
  Protocol protocol(service);std::string session;
  const auto principal="stdio-"+QUuid::createUuid().toString(QUuid::WithoutBraces).toStdString();
  std::mutex outputMutex;
  auto writeReply=[&](const ProtocolReply &reply){if(reply.message){std::lock_guard lock(outputMutex);std::cout<<reply.message->dump()<<'\n'<<std::flush;}};
  struct Task {std::thread thread;std::shared_ptr<std::atomic_bool> done;};std::vector<Task> tasks;
  std::string line;line.reserve(4096);
  auto readLine=[&] {
    line.clear();char ch;
    while(std::cin.get(ch)) {if(ch=='\n') return true;if(line.size()>=1024*1024) throw Error("request_too_large","Stdio line exceeds 1 MiB.",413);line+=ch;}
    return !line.empty();
  };
  int result=0;
  try {
    while(readLine()) {
      std::erase_if(tasks,[](Task &t){if(t.done->load()){t.thread.join();return true;}return false;});
      auto message=Json::parse(line,nullptr,false);
      if(message.is_discarded()){writeReply({200,Json{{"jsonrpc","2.0"},{"id",nullptr},{"error",{{"code",-32700},{"message","Parse error"}}}},session});continue;}
      if(message.is_object()&&message.value("method",Json())=="tools/call"&&tasks.size()<64) {
        auto done=std::make_shared<std::atomic_bool>(false);auto sid=session;
        tasks.push_back({std::thread([&,done,sid,message]{writeReply(protocol.handle(principal,sid,message));*done=true;}),done});
      } else {
        auto reply=protocol.handle(principal,session,message);if(!reply.sessionId.empty()) session=reply.sessionId;writeReply(reply);
      }
    }
  } catch(const std::exception &){std::cerr<<"Stdio input failed.\n";result=1;}
  protocol.shutdown();service.shutdown();for(auto &t:tasks) if(t.thread.joinable()) t.thread.join();return result;
}
}
