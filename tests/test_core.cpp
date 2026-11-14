#include <qws/server.hpp>
#include <qws/html.hpp>
#include <QCoreApplication>
#include <QFile>
#include <QSqlDatabase>
#include <QTemporaryDir>
#include <QUrlQuery>
#include <httplib.h>
#include <future>
#include <iostream>
#include <latch>
#include <stdexcept>
using namespace qws;
namespace {
int assertions=0;
#define REQUIRE(x) do{++assertions;if(!(x))throw std::runtime_error(std::string("Check failed: ")+ #x + " at " + std::to_string(__LINE__));}while(false)
template<class F> void error(const char *code,F f){try{f();}catch(const Error &e){REQUIRE(e.code==code);return;}throw std::runtime_error(std::string("Expected error: ")+code);}
std::shared_ptr<CancelToken> token(int ms=3000){auto t=std::make_shared<CancelToken>();t->deadline=Clock::now()+std::chrono::milliseconds(ms);return t;}
Config config(const QTemporaryDir &dir) {
  return {Json{{"schema_version",1},
    {"business",{{"host","127.0.0.1"},{"port",0},{"allowed_hosts",Json::array({"test.local"})},{"origins",Json::array({"https://client.example"})}}},
    {"control",{{"host","127.0.0.1"},{"port",0},{"allowed_hosts",Json::array({"test.local"})},{"origins",Json::array()}}},
    {"clients",Json::array({Json{{"id","test"},{"token_env","QCWS_TEST_CREDENTIAL"}}})},
    {"provider",{{"url","http://127.0.0.1/search"},{"ca_file",""}}},
    {"cache",{{"directory",dir.path().toStdString()},{"max_bytes",16*1024*1024},{"max_entries",100},{"search_ttl_seconds",300},{"page_ttl_seconds",1800}}},
    {"limits",{{"max_concurrent",5},{"search_timeout_seconds",2},{"open_timeout_seconds",2}}}},dir.path().toStdString()};
}
HttpResponse results(std::string url="https://example.com/a") {
  return {200,Json{{"results",Json::array({{{"title","Example"},{"url",url},{"content","A result"},{"engines",Json::array({"test"})}}})},
    {"unresponsive_engines",Json::array()}}.dump(),"application/json","http://backend/search",{}};
}
ServiceTransport transport() {
  return {[](auto,auto,auto,auto){return results();},[](auto url,auto){return HttpResponse{200,"<title>Title</title><main><p>Hello 中文</p><p>HELLO two</p><a href='/other'>Other</a></main>","text/html",url,{}};}};
}
void networkTests() {
  for(const auto *host:{"0.1.2.3","10.0.0.1","127.0.0.1","169.254.169.254","172.16.0.1","192.168.1.1","100.64.0.1","192.0.0.1",
      "192.0.2.1","198.18.0.1","198.51.100.1","203.0.113.1","224.0.0.1","255.255.255.255","::1","::","fe80::1","fc00::1",
      "2001:db8::1","2002:7f00:1::","64:ff9b::7f00:1","::ffff:127.0.0.1"}) REQUIRE(!HttpClient::isPublicAddress(QHostAddress(host)));
  for(const auto *host:{"1.1.1.1","8.8.8.8","2606:4700:4700::1111","::ffff:8.8.8.8"}) REQUIRE(HttpClient::isPublicAddress(QHostAddress(host)));
  httplib::Server fake;std::atomic_int requests=0;
  fake.Post("/search",[&](const auto &req,auto &res){++requests;res.set_content(req.body,"application/json");});
  fake.Get("/redirect",[](const auto &,auto &res){res.set_redirect("http://127.0.0.1/secret");});
  fake.Get("/large",[](const auto &,auto &res){res.set_content(std::string(5*1024*1024+1,'x'),"text/plain");});
  const int port=fake.bind_to_any_port("127.0.0.1");REQUIRE(port>0);
  std::jthread worker([&]{fake.listen_after_bind();});fake.wait_until_ready();
  struct Stop {httplib::Server &server;~Stop(){server.stop();}} stop{fake};
  const std::string base="http://127.0.0.1:"+std::to_string(port);
  HttpClient client;
  auto response=client.requestBackend(base+"/search","q=C%2B%2B","",token());REQUIRE(response.status==200);REQUIRE(response.body=="q=C%2B%2B");
  error("destination_blocked",[&]{client.getPublic(base+"/search",token());});REQUIRE(requests==1);
  error("destination_blocked",[&]{client.getPublic("http://localhost:"+std::to_string(port)+"/search",token());});
  error("destination_blocked",[&]{client.getPublic("http://2130706433:"+std::to_string(port)+"/search",token());});
  error("invalid_argument",[&]{client.getPublic("file:///etc/passwd",token());});
  error("invalid_argument",[&]{client.getPublic("http://user:password@example.com",token());});
  error("provider_unavailable",[&]{client.requestBackend(base+"/redirect","","",token());});
  error("response_too_large",[&]{client.requestBackend(base+"/large","","",token());});
  auto cancelled=token();cancelled->cancelled=true;error("cancelled",[&]{client.requestBackend(base+"/search","q=x","",cancelled);});
}
void cacheTests() {
  QTemporaryDir dir;REQUIRE(dir.isValid());std::int64_t time=100;const auto baseline=QSqlDatabase::connectionNames().size();
  {
    Cache cache(dir.path().toStdString()+"/cache.sqlite",16*1024*1024,2,[&]{return time;});
    cache.put("a",{{"value",1}},10);cache.put("b",{{"value",2}},10);REQUIRE(cache.get("a")->at("value")==1);
    cache.put("c",{{"value",3}},10);REQUIRE(!cache.get("b"));REQUIRE(cache.get("c"));
    time=110;REQUIRE(!cache.get("a"));REQUIRE(cache.stats()["entries"]==0);
    error("cache_unavailable",[&]{Cache other(dir.path().toStdString()+"/cache.sqlite");});
    for(int i=0;i<100;++i) cache.put(std::to_string(i),{{"data",std::string(100000,'a')}},60);
    REQUIRE(cache.stats()["storage_bytes"].get<long long>()<=16*1024*1024);
    cache.close();cache.close();error("cache_unavailable",[&]{cache.get("a");});
  }
  REQUIRE(QSqlDatabase::connectionNames().size()==baseline);
  for(int i=0;i<10;++i) error("cache_unavailable",[&]{Cache bad(dir.path().toStdString());});
  REQUIRE(QSqlDatabase::connectionNames().size()==baseline);
}
void htmlTests() {
  auto p=parseHtml("<title>T &amp; T</title><nav>omit</nav><main><p>A &amp; B</p><p hidden>secret</p><p>中文</p><script>bad</script><a href='/x'>Link</a></main>","https://example.com/a","text/html");
  REQUIRE(p.title=="T & T");REQUIRE(p.text=="A & B\n中文\nLink");REQUIRE(p.links[0]["url"]=="https://example.com/x");
  REQUIRE(parseHtml(std::string("\xd6\xd0\xce\xc4",4),"https://example.com","text/plain; charset=GBK").text=="中文");
  REQUIRE(parseHtml(std::string("\xa4\xa4\xa4\xe5",4),"https://example.com","text/plain; charset=Big5").text=="中文");
  REQUIRE(parseHtml(std::string("\x80",1),"https://example.com","text/plain; charset=ISO-8859-1").text=="€");
  REQUIRE(parseHtml("<meta charset='GBK'><p>"+std::string("\xd6\xd0\xce\xc4",4)+"</p>","https://example.com","text/html").text=="中文");
  REQUIRE(parseHtml(std::string("\xef\xbb\xbf",3)+"中文","https://example.com","text/plain; charset=windows-1252").text=="中文");
  error("unsupported_content",[]{parseHtml("a","https://example.com","application/pdf");});
  error("unsupported_content",[]{parseHtml("a","https://example.com","text/html; charset=invalid");});
  error("unsupported_content",[]{parseHtml(std::string("\xf0\x9f",2),"https://example.com","text/plain");});
  std::string emoji;for(int i=0;i<1001;++i) emoji+="😀";
  auto wrapped=parseHtml(emoji,"https://example.com","text/plain");REQUIRE(QString::fromStdString(wrapped.text).split('\n').size()==2);
  REQUIRE(QString::fromStdString(wrapped.text).split('\n')[0].toUcs4().size()==1000);
  std::string links;for(int i=0;i<201;++i) links+="<a href='/"+std::to_string(i)+"'>x</a>";
  auto many=parseHtml(links,"https://example.com","text/html");REQUIRE(many.links.size()==200);REQUIRE(many.truncated);
  std::string deep;for(int i=0;i<514;++i) deep+="<div>";deep+='x';error("response_too_large",[&]{parseHtml(deep,"https://example.com","text/html");});
}
void serviceTests() {
  QTemporaryDir dir;auto t=transport();std::atomic_int searches=0;std::string form;
  t.search=[&](auto,auto body,auto,auto){++searches;form=body;return results();};
  Service s(config(dir),t);
  auto r=s.call("web_search",{{"query","C++ 100%25 中文 a&b"},{"limit",1}},"alice",token());REQUIRE(r["status"]=="ok");
  QUrlQuery q(QString::fromStdString(form));REQUIRE(q.queryItemValue("q",QUrl::FullyDecoded)=="C++ 100%25 中文 a&b");
  r=s.call("web_search",{{"query","C++ 100%25 中文 a&b"},{"limit",1}},"alice",token());REQUIRE(r["cache"]["hit"]==true);REQUIRE(searches==1);
  s.call("web_search",{{"query","C++ 100%25 中文 a&b"},{"limit",1}},"bob",token());REQUIRE(searches==2);
  r=s.call("web_search",{{"query","q"},{"domains",Json::array({"EXAMPLE.COM."})}},"alice",token());REQUIRE(r["results"].size()==1);
  r=s.call("web_search",{{"query","q"},{"domains",Json::array({"other.example"})}},"alice",token());REQUIRE(r["results"].empty());
  std::string largeQuery;for(int i=0;i<2048;++i) largeQuery+="😀";
  REQUIRE(s.call("web_search",{{"query",largeQuery}},"alice",token())["results"].size()==1);
  auto p=s.call("web_open",{{"url","https://example.com/a"},{"max_lines",1}},"alice",token());REQUIRE(p["lines"].size()==1);REQUIRE(p["next_start_line"]==2);
  const auto id=p["page_id"];
  REQUIRE(s.call("web_open",{{"page_id",id},{"start_line",2}},"alice",token())["lines"][0]["line"]==2);
  auto f=s.call("web_find",{{"page_id",id},{"pattern","hello"},{"max_matches",1}},"alice",token());REQUIRE(f["matches"].size()==1);REQUIRE(f["next_start_line"]==2);
  f=s.call("web_find",{{"page_id",id},{"pattern","hello"},{"start_line",2}},"alice",token());REQUIRE(f["matches"][0]["line"]==2);
  error("page_unavailable",[&]{s.call("web_find",{{"page_id",id},{"pattern","hello"}},"bob",token());});
  error("invalid_argument",[&]{s.call("web_open",{{"url","https://example.com"},{"page_id",id}},"alice",token());});
  REQUIRE(s.status()["active_calls"]==0);
}
void concurrencyTests() {
  QTemporaryDir dir;auto t=transport();std::latch entered(5);std::atomic_bool release=false;
  t.search=[&](auto,auto,auto,auto c){if(!release) entered.count_down();while(!release&&!c->stopped()) std::this_thread::sleep_for(std::chrono::milliseconds(5));return results();};
  Service s(config(dir),t);std::vector<std::future<Json>> work;
  for(int i=0;i<5;++i) work.push_back(std::async(std::launch::async,[&,i]{return s.call("web_search",{{"query",std::to_string(i)},{"limit",1}},"alice",token());}));
  entered.wait();const auto start=Clock::now();error("busy",[&]{s.call("web_search",{{"query","six"}},"alice",token());});
  REQUIRE(Clock::now()-start<std::chrono::milliseconds(200));REQUIRE(s.status()["active_calls"]==5);
  release=true;for(auto &f:work) REQUIRE(f.get()["results"].size()==1);
  REQUIRE(s.call("web_search",{{"query","after"}},"alice",token())["status"]=="ok");REQUIRE(s.status()["active_calls"]==0);
}
void protocolTests() {
  QTemporaryDir dir;Service s(config(dir),transport());Protocol p(s);
  Json init={{"jsonrpc","2.0"},{"id","1"},{"method","initialize"},{"params",{{"protocolVersion","2025-06-18"},{"capabilities",Json::object()},{"clientInfo",{{"name","test"},{"version","1"}}}}}};
  auto r=p.handle("alice","",init);REQUIRE(r.httpStatus==200);REQUIRE(!r.sessionId.empty());const auto sid=r.sessionId;
  r=p.handle("alice",sid,{{"jsonrpc","2.0"},{"id",1},{"method","tools/list"}});REQUIRE(r.message->contains("error"));
  r=p.handle("alice",sid,{{"jsonrpc","2.0"},{"method","notifications/initialized"}});REQUIRE(r.httpStatus==202);REQUIRE(!r.message);
  r=p.handle("alice",sid,{{"jsonrpc","2.0"},{"id",2},{"method","tools/list"}});REQUIRE((*r.message)["result"]["tools"].size()==4);
  r=p.handle("bob",sid,{{"jsonrpc","2.0"},{"id",3},{"method","tools/list"}});REQUIRE(r.httpStatus==404);
  r=p.handle("alice",sid,{{"jsonrpc","2.0"},{"id",3},{"method","tools/call"},{"params",{{"name","web_find"},{"arguments",{{"page_id","missing"},{"pattern","x"}}}}}});
  REQUIRE((*r.message)["result"]["isError"]==true);REQUIRE((*r.message)["result"]["structuredContent"]["error"]["code"]=="page_unavailable");
  REQUIRE(p.deleteSession("alice",sid).httpStatus==204);REQUIRE(p.deleteSession("alice",sid).httpStatus==404);
  REQUIRE(Protocol::validateJsonRpc(Json::array()).has_value());REQUIRE(Protocol::validateJsonRpc({{"jsonrpc","2.0"},{"method","ping"},{"id",true}}).has_value());
}
void httpTests() {
  qputenv("QCWS_TEST_CREDENTIAL",QByteArray(64,'a'));
  QTemporaryDir dir;Service service(config(dir),transport());HttpServer server(service,config(dir));server.start();
  httplib::Client client("127.0.0.1",server.businessPort());client.set_connection_timeout(2);client.set_read_timeout(3);
  httplib::Headers headers={{"Host","test.local"},{"Authorization","Bearer "+std::string(64,'a')}};
  auto r=client.Get("/v1/status",headers);REQUIRE(r&&r->status==200);REQUIRE(Json::parse(r->body)["contract_version"]==1);
  r=client.Get("/v1/status",httplib::Headers{{"Host","test.local"}});REQUIRE(r&&r->status==401);
  auto evil=headers;evil.emplace("Origin","https://evil.example");r=client.Get("/v1/status",evil);REQUIRE(r&&r->status==403);
  r=client.Options("/mcp",{{"Host","test.local"},{"Origin","https://client.example"},{"Access-Control-Request-Method","POST"}});REQUIRE(r&&r->status==204);
  r=client.Post("/v1/search",headers,"{\"query\":\"hello\"}","application/json");REQUIRE(r&&r->status==200);
  r=client.Get("/mcp",headers);REQUIRE(r&&r->status==405);
  r=client.Post("/mcp",headers,"{}","application/json");REQUIRE(r&&r->status==406);
  httplib::Client control("127.0.0.1",server.controlPort());r=control.Get("/health/live",headers);REQUIRE(r&&r->status==200);
  server.stop();qunsetenv("QCWS_TEST_CREDENTIAL");
}
}
int main(int argc,char **argv) {
  QCoreApplication app(argc,argv);
  const std::pair<const char*,void(*)()> tests[]={{"network",networkTests},{"cache",cacheTests},{"html",htmlTests},{"service",serviceTests},{"concurrency",concurrencyTests},{"protocol",protocolTests},{"http",httpTests}};
  int failed=0;
  for(auto [name,fn]:tests) try{fn();std::cout<<"PASS "<<name<<'\n';}catch(const std::exception &e){++failed;std::cerr<<"FAIL "<<name<<": "<<e.what()<<'\n';}
  std::cout<<assertions<<" assertions, "<<failed<<" failed suites\n";return failed?1:0;
}
