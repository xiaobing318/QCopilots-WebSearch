#include <QCoreApplication>
#include <QFile>
#include <QLockFile>
#include <QProcess>
#include <QSqlDatabase>
#include <QSqlQuery>
#include <QStringDecoder>
#include <QTemporaryDir>
#include <QUrlQuery>
#include <QUuid>
#include <array>
#include <future>
#include <httplib.h>
#include <iostream>
#include <latch>
#include <qws/html.hpp>
#include <qws/server.hpp>
#include <stdexcept>
#include <zlib.h>
using namespace qws;
namespace {
int assertions = 0;
#define REQUIRE(x)                                                                                           \
  do {                                                                                                       \
    ++assertions;                                                                                            \
    if (!(x))                                                                                                \
      throw std::runtime_error(std::string("Check failed: ") + #x + " at " + std::to_string(__LINE__));      \
  } while (false)
template <class F> void error(const char *code, F f) {
  try {
    f();
  } catch (const Error &e) {
    REQUIRE(e.code == code);
    return;
  }
  throw std::runtime_error(std::string("Expected error: ") + code);
}
std::shared_ptr<CancelToken> token(int ms = 3000) {
  auto t = std::make_shared<CancelToken>();
  t->deadline = Clock::now() + std::chrono::milliseconds(ms);
  return t;
}
Config config(const QTemporaryDir &dir) {
  return {
      Json{{"schema_version", 1},
           {"business",
            {{"host", "127.0.0.1"},
             {"port", 0},
             {"allowed_hosts", Json::array({"test.local"})},
             {"origins", Json::array({"https://client.example"})}}},
           {"control",
            {{"host", "127.0.0.1"},
             {"port", 0},
             {"allowed_hosts", Json::array({"test.local"})},
             {"origins", Json::array()}}},
           {"clients", Json::array({Json{{"id", "test"}, {"token_env", "QCWS_TEST_CREDENTIAL"}}})},
           {"provider", {{"url", "http://127.0.0.1/search"}, {"ca_file", ""}}},
           {"cache",
            {{"directory", dir.path().toStdString()},
             {"max_bytes", 16 * 1024 * 1024},
             {"max_entries", 100},
             {"search_ttl_seconds", 300},
             {"page_ttl_seconds", 1800}}},
           {"limits", {{"max_concurrent", 5}, {"search_timeout_seconds", 2}, {"open_timeout_seconds", 2}}}},
      dir.path().toStdString()};
}
HttpResponse results(std::string url = "https://example.com/a") {
  return {200,
          Json{{"results", Json::array({{{"title", "Example"},
                                         {"url", url},
                                         {"content", "A result"},
                                         {"engines", Json::array({"test"})}}})},
               {"unresponsive_engines", Json::array()}}
              .dump(),
          "application/json",
          "http://backend/search",
          {}};
}
ServiceTransport transport() {
  return {
      [](auto, auto, auto, auto) { return results(); },
      [](auto url, auto) {
        return HttpResponse{
            200,
            "<title>Title</title><main><p>Hello 中文</p><p>HELLO two</p><a href='/other'>Other</a></main>",
            "text/html",
            url,
            {}};
      }};
}
void networkTests() {
  for (const auto *host : {"0.1.2.3",         "10.0.0.1",        "127.0.0.1",    "169.254.169.254",
                           "172.16.0.1",      "192.168.1.1",     "100.64.0.1",   "192.0.0.1",
                           "192.0.2.1",       "198.18.0.1",      "198.51.100.1", "203.0.113.1",
                           "224.0.0.1",       "255.255.255.255", "::1",          "::",
                           "fe80::1",         "fc00::1",         "2001:db8::1",  "2002:7f00:1::",
                           "64:ff9b::7f00:1", "::ffff:127.0.0.1"})
    REQUIRE(!HttpClient::isPublicAddress(QHostAddress(host)));
  for (const auto *host : {"1.1.1.1", "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"})
    REQUIRE(HttpClient::isPublicAddress(QHostAddress(host)));
  httplib::Server fake;
  std::atomic_int requests = 0;
  fake.Post("/search", [&](const auto &req, auto &res) {
    ++requests;
    res.set_content(req.body, "application/json");
  });
  fake.Get("/redirect", [](const auto &, auto &res) { res.set_redirect("http://127.0.0.1/secret"); });
  fake.Get("/large", [](const auto &, auto &res) {
    res.set_content(std::string(5 * 1024 * 1024 + 1, 'x'), "text/plain");
  });
  const int port = fake.bind_to_any_port("127.0.0.1");
  REQUIRE(port > 0);
  std::jthread worker([&] { fake.listen_after_bind(); });
  fake.wait_until_ready();
  struct Stop {
    httplib::Server &server;
    ~Stop() {
      server.stop();
    }
  } stop{fake};
  const std::string base = "http://127.0.0.1:" + std::to_string(port);
  HttpClient client;
  auto response = client.requestBackend(base + "/search", "q=C%2B%2B", "", token());
  REQUIRE(response.status == 200);
  REQUIRE(response.body == "q=C%2B%2B");
  error("destination_blocked", [&] { client.getPublic(base + "/search", token()); });
  REQUIRE(requests == 1);
  error("destination_blocked",
        [&] { client.getPublic("http://localhost:" + std::to_string(port) + "/search", token()); });
  error("destination_blocked",
        [&] { client.getPublic("http://2130706433:" + std::to_string(port) + "/search", token()); });
  error("destination_blocked",
        [&] { client.getPublic("http://[::ffff:127.0.0.1]:" + std::to_string(port) + "/search", token()); });
  REQUIRE(requests == 1);
  error("invalid_argument", [&] { client.getPublic("file:///etc/passwd", token()); });
  error("invalid_argument", [&] { client.getPublic("http://user:password@example.com", token()); });
  error("provider_unavailable", [&] { client.requestBackend(base + "/redirect", "", "", token()); });
  error("response_too_large", [&] { client.requestBackend(base + "/large", "", "", token()); });
  auto cancelled = token();
  cancelled->cancelled = true;
  error("cancelled", [&] { client.requestBackend(base + "/search", "q=x", "", cancelled); });
}
template <class Predicate> bool eventually(Predicate predicate, int milliseconds = 2000) {
  const auto deadline = Clock::now() + std::chrono::milliseconds(milliseconds);
  do {
    if (predicate())
      return true;
    std::this_thread::sleep_for(std::chrono::milliseconds(2));
  } while (Clock::now() < deadline);
  return predicate();
}
struct LocalHttpFixture {
  httplib::Server server;
  std::jthread thread;
  int port = 0;
  void start() {
    server.set_compress(false);
    server.set_read_timeout(1);
    server.set_write_timeout(1);
    port = server.bind_to_any_port("127.0.0.1");
    if (port <= 0)
      throw std::runtime_error("Cannot bind HTTP fixture");
    thread = std::jthread([this] { server.listen_after_bind(); });
    server.wait_until_ready();
  }
  std::string url(const std::string &path) const {
    return "http://127.0.0.1:" + std::to_string(port) + path;
  }
  ~LocalHttpFixture() {
    server.stop();
    if (thread.joinable())
      thread.join();
  }
};
std::string gzipFixture(const std::string &plain, std::size_t filenameBytes = 0) {
  z_stream stream{};
  if (deflateInit2(&stream, Z_DEFAULT_COMPRESSION, Z_DEFLATED, MAX_WBITS + 16, 8, Z_DEFAULT_STRATEGY) != Z_OK)
    throw std::runtime_error("Cannot initialize fixture compressor");
  struct End {
    z_stream *s;
    ~End() {
      deflateEnd(s);
    }
  } end{&stream};
  stream.next_in = reinterpret_cast<Bytef *>(const_cast<char *>(plain.data()));
  stream.avail_in = static_cast<uInt>(plain.size());
  std::array<char, 16384> buffer{};
  std::string encoded;
  int status;
  do {
    stream.next_out = reinterpret_cast<Bytef *>(buffer.data());
    stream.avail_out = static_cast<uInt>(buffer.size());
    status = deflate(&stream, Z_FINISH);
    if (status != Z_OK && status != Z_STREAM_END)
      throw std::runtime_error("Fixture compression failed");
    encoded.append(buffer.data(), buffer.size() - stream.avail_out);
  } while (status != Z_STREAM_END);
  if (filenameBytes) {
    if (encoded.size() < 10 || encoded[3] != 0)
      throw std::runtime_error("Unexpected gzip header");
    encoded[3] = char(8);
    encoded.insert(10, std::string(filenameBytes, 'n') + '\0');
  }
  return encoded;
}
std::size_t inflatedSize(const std::string &encoded) {
  z_stream stream{};
  if (inflateInit2(&stream, MAX_WBITS + 16) != Z_OK)
    throw std::runtime_error("Cannot initialize inflater");
  struct End {
    z_stream *s;
    ~End() {
      inflateEnd(s);
    }
  } end{&stream};
  stream.next_in = reinterpret_cast<Bytef *>(const_cast<char *>(encoded.data()));
  stream.avail_in = static_cast<uInt>(encoded.size());
  std::array<char, 16384> buffer{};
  std::size_t size = 0;
  int status;
  do {
    stream.next_out = reinterpret_cast<Bytef *>(buffer.data());
    stream.avail_out = static_cast<uInt>(buffer.size());
    status = inflate(&stream, Z_NO_FLUSH);
    if (status != Z_OK && status != Z_STREAM_END)
      throw std::runtime_error("Invalid gzip fixture");
    size += buffer.size() - stream.avail_out;
  } while (status != Z_STREAM_END);
  REQUIRE(stream.avail_in == 0);
  return size;
}
void networkRegressionTests() {
  constexpr std::size_t limit = 5 * 1024 * 1024;
  auto exact = std::make_shared<std::string>(gzipFixture(std::string(limit, 'x')));
  auto expanded = std::make_shared<std::string>(gzipFixture(std::string(limit + 1, 'x')));
  auto wire = std::make_shared<std::string>(gzipFixture("ok", limit + 64));
  REQUIRE(exact->size() < limit);
  REQUIRE(expanded->size() < limit);
  REQUIRE(wire->size() > limit);
  REQUIRE(inflatedSize(*exact) == limit);
  REQUIRE(inflatedSize(*expanded) == limit + 1);
  REQUIRE(inflatedSize(*wire) == 2);
  LocalHttpFixture fixture;
  for (auto [path, data] : std::vector<std::pair<std::string, std::shared_ptr<std::string>>>{
           {"/exact", exact}, {"/expanded", expanded}, {"/wire", wire}}) {
    fixture.server.Get(path, [data](const auto &, auto &response) {
      response.set_header("Content-Encoding", "gzip");
      response.set_chunked_content_provider(
          "text/plain", [data](std::size_t offset, httplib::DataSink &sink) {
            if (offset >= data->size()) {
              sink.done();
              return true;
            }
            return sink.write(data->data() + offset, std::min<std::size_t>(16384, data->size() - offset));
          });
    });
  }
  std::atomic_int redirects = 0, targets = 0;
  std::atomic_bool authorized = true, entered = false;
  fixture.server.Get("/ok", [](const auto &, auto &r) { r.set_content("ok", "text/plain"); });
  fixture.server.Get("/header", [](const auto &, auto &r) {
    r.set_header("X-Fixture", std::string(64 * 1024, 'h'));
    r.set_content("ok", "text/plain");
  });
  fixture.server.Post("/redirect/([0-9]+)", [&](const auto &req, auto &r) {
    ++redirects;
    if (req.get_header_value("Authorization") != "Bearer fixture-secret")
      authorized = false;
    r.set_redirect("/target", std::stoi(req.matches[1].str()));
  });
  fixture.server.Get("/target", [&](const auto &, auto &r) {
    ++targets;
    r.set_content("unexpected", "text/plain");
  });
  fixture.server.Post("/target", [&](const auto &, auto &r) {
    ++targets;
    r.set_content("unexpected", "text/plain");
  });
  fixture.server.Get("/hold", [&](const auto &req, auto &r) {
    entered = true;
    const auto end = Clock::now() + std::chrono::seconds(2);
    while (Clock::now() < end && !req.is_connection_closed())
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    r.set_content("late", "text/plain");
  });
  fixture.start();
  HttpClient client;
  auto r = client.requestBackend(fixture.url("/exact"), "", "", token(5000));
  REQUIRE(r.body.size() == limit);
  for (const auto *route : {"/expanded", "/wire", "/header"})
    error("response_too_large", [&] { client.requestBackend(fixture.url(route), "", "", token(5000)); });
  for (int status : {301, 302, 303, 307, 308})
    error("provider_unavailable", [&] {
      client.requestBackend(fixture.url("/redirect/" + std::to_string(status)), "q=redirect",
                            "fixture-secret", token());
    });
  REQUIRE(redirects == 5);
  REQUIRE(authorized);
  REQUIRE(targets == 0);
  auto cancellation = token(5000);
  auto pending = std::async(std::launch::async, [&] {
    try {
      client.requestBackend(fixture.url("/hold"), "", "", cancellation);
      return std::string("unexpected");
    } catch (const Error &e) {
      return e.code;
    }
  });
  REQUIRE(eventually([&] { return entered.load(); }));
  cancellation->cancelled = true;
  REQUIRE(pending.wait_for(std::chrono::seconds(1)) == std::future_status::ready);
  REQUIRE(pending.get() == "cancelled");
  REQUIRE(client.requestBackend(fixture.url("/ok"), "", "", token()).body == "ok");
  const auto start = Clock::now();
  error("timeout", [&] { client.requestBackend(fixture.url("/hold"), "", "", token(100)); });
  REQUIRE(Clock::now() - start < std::chrono::seconds(1));
  REQUIRE(!HttpClient::isPublicAddress(QHostAddress("168.63.129.16")));
  REQUIRE(!HttpClient::isPublicAddress(QHostAddress("::ffff:168.63.129.16")));
}
void coalescingTests() {
  std::atomic_int requests = 0, disconnected = 0;
  std::atomic_bool release = false;
  LocalHttpFixture fixture;
  fixture.server.Post("/search", [&](const auto &request, auto &response) {
    ++requests;
    const auto end = Clock::now() + std::chrono::seconds(8);
    while (!release && Clock::now() < end) {
      if (request.is_connection_closed()) {
        ++disconnected;
        return;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    response.set_content(results().body, "application/json");
  });
  fixture.start();
  QTemporaryDir dir;
  auto settings = config(dir);
  settings.value["provider"]["url"] = fixture.url("/search");
  settings.value["limits"]["search_timeout_seconds"] = 8;
  Service service(settings);
  struct Outcome {
    Json value;
    std::string code;
  };
  auto launch = [&](std::string query, std::shared_ptr<CancelToken> cancel) {
    return std::async(std::launch::async, [&, query, cancel]() -> Outcome {
      try {
        return {service.call("web_search", {{"query", query}, {"limit", 1}}, "alice", cancel), {}};
      } catch (const Error &e) {
        return {{}, e.code};
      }
    });
  };
  auto joined = [&] { return service.status()["search_waiters"] == 2; };
  auto idle = [&] {
    auto s = service.status();
    return s["active_calls"] == 0 && s["active_upstream"] == 0;
  };
  auto a = token(5000), b = token(5000);
  auto first = launch("survivor", a), second = launch("survivor", b);
  REQUIRE(eventually(joined));
  REQUIRE(eventually([&] { return requests == 1; }));
  a->cancelled = true;
  REQUIRE(first.wait_for(std::chrono::seconds(1)) == std::future_status::ready);
  REQUIRE(first.get().code == "cancelled");
  REQUIRE(second.wait_for(std::chrono::milliseconds(0)) == std::future_status::timeout);
  REQUIRE(disconnected == 0);
  release = true;
  REQUIRE(second.wait_for(std::chrono::seconds(2)) == std::future_status::ready);
  auto survivor = second.get();
  REQUIRE(survivor.code.empty());
  REQUIRE(survivor.value["results"].size() == 1);
  REQUIRE(eventually(idle));
  REQUIRE(service.call("web_search", {{"query", "survivor"}, {"limit", 1}}, "alice",
                       token())["cache"]["hit"] == true);
  REQUIRE(requests == 1);
  release = false;
  a = token(5000);
  b = token(5000);
  auto third = launch("cancel-all", a), fourth = launch("cancel-all", b);
  REQUIRE(eventually(joined));
  REQUIRE(eventually([&] { return requests == 2; }));
  a->cancelled = true;
  b->cancelled = true;
  REQUIRE(third.get().code == "cancelled");
  REQUIRE(fourth.get().code == "cancelled");
  REQUIRE(eventually(idle));
  REQUIRE(eventually([&] { return disconnected >= 1; }));
  release = true;
  auto retry = service.call("web_search", {{"query", "cancel-all"}, {"limit", 1}}, "alice", token());
  REQUIRE(retry["cache"]["hit"] == false);
  REQUIRE(requests == 3);
  REQUIRE(eventually(idle));
}
void cacheTests() {
  QTemporaryDir dir;
  REQUIRE(dir.isValid());
  std::int64_t time = 100;
  const auto baseline = QSqlDatabase::connectionNames().size();
  {
    Cache cache(dir.path().toStdString() + "/cache.sqlite", 16 * 1024 * 1024, 2, [&] { return time; });
    cache.put("a", {{"value", 1}}, 10);
    cache.put("b", {{"value", 2}}, 10);
    REQUIRE(cache.get("a")->at("value") == 1);
    cache.put("c", {{"value", 3}}, 10);
    REQUIRE(!cache.get("b"));
    REQUIRE(cache.get("c"));
    time = 110;
    REQUIRE(!cache.get("a"));
    REQUIRE(cache.stats()["entries"] == 0);
    error("cache_unavailable", [&] { Cache other(dir.path().toStdString() + "/cache.sqlite"); });
    for (int i = 0; i < 100; ++i)
      cache.put(std::to_string(i), {{"data", std::string(100000, 'a')}}, 60);
    REQUIRE(cache.stats()["storage_bytes"].get<long long>() <= 16 * 1024 * 1024);
    cache.close();
    cache.close();
    error("cache_unavailable", [&] { cache.get("a"); });
  }
  REQUIRE(QSqlDatabase::connectionNames().size() == baseline);
  for (int i = 0; i < 10; ++i)
    error("cache_unavailable", [&] { Cache bad(dir.path().toStdString()); });
  REQUIRE(QSqlDatabase::connectionNames().size() == baseline);
}
struct SqlFixture {
  QString name = "fixture-" + QUuid::createUuid().toString();
  QSqlDatabase db;
  explicit SqlFixture(const QString &path) : db(QSqlDatabase::addDatabase("QSQLITE", name)) {
    db.setDatabaseName(path);
    if (!db.open()) {
      db = QSqlDatabase();
      QSqlDatabase::removeDatabase(name);
      throw std::runtime_error("Cannot open SQLite fixture");
    }
  }
  ~SqlFixture() {
    db.close();
    db = QSqlDatabase();
    QSqlDatabase::removeDatabase(name);
  }
  void execute(const QString &sql) {
    QSqlQuery query(db);
    REQUIRE(query.exec(sql));
  }
};
void cacheFailureTests() {
  QTemporaryDir dir;
  REQUIRE(dir.isValid());
  const auto baseline = QSqlDatabase::connectionNames().size();
  const auto path = dir.filePath("faults.sqlite");
  auto probe = [&](const QString &expect) {
    QProcess child;
    child.start(QCoreApplication::applicationFilePath(), {"--cache-lock-probe", path, expect});
    if (!child.waitForFinished(5000)) {
      child.kill();
      child.waitForFinished(2000);
      throw std::runtime_error("Cache lock probe timed out");
    }
    REQUIRE(child.exitStatus() == QProcess::NormalExit);
    REQUIRE(child.exitCode() == 0);
  };
  {
    Cache cache(path.toStdString(), 16 * 1024 * 1024, 100, [] { return std::int64_t(100); });
    cache.put("stable", {{"value", "original"}}, 60);
    probe("locked");
    SqlFixture fixture(path);
    fixture.execute("BEGIN IMMEDIATE");
    REQUIRE(cache.get("stable")->at("value") == "original");
    const auto began = Clock::now();
    error("cache_unavailable", [&] { cache.put("stable", {{"value", "replacement"}}, 60); });
    REQUIRE(Clock::now() - began < std::chrono::seconds(2));
    fixture.execute("ROLLBACK");
    REQUIRE(cache.get("stable")->at("value") == "original");
    cache.put("after-busy", {{"ok", true}}, 60);
    REQUIRE(cache.get("after-busy")->at("ok") == true);
    fixture.execute("CREATE TABLE fixture_filler(data BLOB)");
    fixture.execute("INSERT INTO fixture_filler VALUES(zeroblob(3*1024*1024))");
    Json large = {{"data", std::string(2 * 1024 * 1024, 'x')}};
    REQUIRE(large.dump().size() < cache.stats()["effective_payload_limit"].get<std::size_t>());
    error("cache_unavailable", [&] { cache.put("stable", large, 60); });
    REQUIRE(cache.get("stable")->at("value") == "original");
    fixture.execute("DROP TABLE fixture_filler");
    cache.put("stable", large, 60);
    REQUIRE(cache.get("stable")->at("data").get<std::string>().size() == 2 * 1024 * 1024);
    fixture.execute("UPDATE entries SET payload=X'7B',size=1");
    REQUIRE(!cache.get("stable"));
    cache.put("recovered", {{"ok", true}}, 60);
    REQUIRE(cache.get("recovered")->at("ok") == true);
  }
  probe("open");
  const auto lruPath = dir.filePath("lru.sqlite");
  {
    Cache cache(lruPath.toStdString(), 16 * 1024 * 1024, 2, [] { return std::int64_t(100); });
    cache.put("a", {{"value", 1}}, 60);
    cache.put("b", {{"value", 2}}, 60);
    {
      SqlFixture fixture(lruPath);
      fixture.execute("BEGIN IMMEDIATE");
      REQUIRE(cache.get("a")->at("value") == 1);
      error("cache_unavailable", [&] { cache.put("c", {{"value", 3}}, 60); });
      fixture.execute("ROLLBACK");
      fixture.execute("CREATE TRIGGER fixture_fail_insert BEFORE INSERT ON entries "
                      "BEGIN SELECT RAISE(ABORT,'fixture'); END");
      error("cache_unavailable", [&] { cache.put("c", {{"value", 3}}, 60); });
      fixture.execute("DROP TRIGGER fixture_fail_insert");
    }
    cache.put("c", {{"value", 3}}, 60);
    REQUIRE(!cache.get("b"));
    REQUIRE(cache.get("a"));
  }
  {
    Cache reopened(lruPath.toStdString(), 16 * 1024 * 1024, 2, [] { return std::int64_t(100); });
    reopened.put("d", {{"value", 4}}, 60);
    REQUIRE(reopened.get("a"));
    REQUIRE(!reopened.get("c"));
  }
  REQUIRE(QSqlDatabase::connectionNames().size() == baseline);
  const auto corrupt = dir.filePath("corrupt.sqlite");
  {
    QFile file(corrupt);
    REQUIRE(file.open(QIODevice::WriteOnly));
    REQUIRE(file.write("not a SQLite database") == 21);
  }
  error("cache_unavailable", [&] { Cache broken(corrupt.toStdString()); });
  REQUIRE(QSqlDatabase::connectionNames().size() == baseline);
  QLockFile lock(corrupt + ".lock");
  REQUIRE(lock.tryLock(0));
}
void cachePhysicalPressureTests() {
  QTemporaryDir dir;
  REQUIRE(dir.isValid());
  const auto path = dir.filePath("physical.sqlite");
  Cache cache(path.toStdString(), 16 * 1024 * 1024, 10000, [] { return std::int64_t(100); });
  const Json payload{{"data", std::string(2100, 'x')}};
  cache.put("hot", payload, 60);
  for (int i = 0; i < 2000; ++i) {
    if (i == 970)
      for (int j = 0; j < i; ++j)
        if (auto entry = cache.get("item-" + std::to_string(j)))
          REQUIRE(*entry == payload);
    REQUIRE(cache.get("hot") == payload);
    cache.put("item-" + std::to_string(i), payload, 60);
  }
  REQUIRE(cache.get("hot") == payload);
  REQUIRE(cache.get("item-1999") == payload);
  REQUIRE(!cache.get("item-0"));
  REQUIRE(cache.stats()["storage_bytes"].get<long long>() <= 16 * 1024 * 1024);
  cache.close();
  Cache reopened(path.toStdString(), 16 * 1024 * 1024, 10000, [] { return std::int64_t(100); });
  REQUIRE(reopened.get("hot") == payload);
  REQUIRE(reopened.get("item-1999") == payload);
  reopened.close();
  Cache reduced(path.toStdString(), 16 * 1024 * 1024, 1, [] { return std::int64_t(100); });
  for (int i = 0; i < 2000; ++i)
    reduced.get("item-" + std::to_string(i));
  REQUIRE(reduced.get("hot") == payload);
  reduced.put("replacement", payload, 60);
  REQUIRE(reduced.get("replacement") == payload);
  REQUIRE(reduced.stats()["entries"] == 1);
}
void htmlTests() {
  auto p = parseHtml("<title>T &amp; T</title><nav>omit</nav><main><p>A &amp; B</p><p "
                     "hidden>secret</p><p>中文</p><script>bad</script><a href='/x'>Link</a></main>",
                     "https://example.com/a", "text/html");
  REQUIRE(p.title == "T & T");
  REQUIRE(p.text == "A & B\n中文\nLink");
  REQUIRE(p.links[0]["url"] == "https://example.com/x");
  REQUIRE(
      parseHtml(std::string("\xd6\xd0\xce\xc4", 4), "https://example.com", "text/plain; charset=GBK").text ==
      "中文");
  REQUIRE(
      parseHtml(std::string("\xa4\xa4\xa4\xe5", 4), "https://example.com", "text/plain; charset=Big5").text ==
      "中文");
  REQUIRE(parseHtml(std::string("\x80", 1), "https://example.com", "text/plain; charset=ISO-8859-1").text ==
          "€");
  REQUIRE(parseHtml("<meta charset='GBK'><p>" + std::string("\xd6\xd0\xce\xc4", 4) + "</p>",
                    "https://example.com", "text/html")
              .text == "中文");
  REQUIRE(parseHtml(std::string("\xef\xbb\xbf", 3) + "中文", "https://example.com",
                    "text/plain; charset=windows-1252")
              .text == "中文");
  error("unsupported_content", [] { parseHtml("a", "https://example.com", "application/pdf"); });
  error("unsupported_content", [] { parseHtml("a", "https://example.com", "text/html; charset=invalid"); });
  error("unsupported_content",
        [] { parseHtml(std::string("\xf0\x9f", 2), "https://example.com", "text/plain"); });
  std::string emoji;
  for (int i = 0; i < 1001; ++i)
    emoji += "😀";
  auto wrapped = parseHtml(emoji, "https://example.com", "text/plain");
  REQUIRE(QString::fromStdString(wrapped.text).split('\n').size() == 2);
  REQUIRE(QString::fromStdString(wrapped.text).split('\n')[0].toUcs4().size() == 1000);
  std::string links;
  for (int i = 0; i < 201; ++i)
    links += "<a href='/" + std::to_string(i) + "'>x</a>";
  auto many = parseHtml(links, "https://example.com", "text/html");
  REQUIRE(many.links.size() == 200);
  REQUIRE(many.truncated);
  std::string deep;
  for (int i = 0; i < 514; ++i)
    deep += "<div>";
  deep += 'x';
  error("response_too_large", [&] { parseHtml(deep, "https://example.com", "text/html"); });
}
void htmlBoundaryTests() {
  auto read = [](const std::string &s, const std::string &type = "text/html") {
    return parseHtml(s, "https://example.com/a", type);
  };
  // Regression from the HTML fuzz campaign: control removal moves a valid
  // embedded U+FEFF to the first output scalar. Default Qt decoding strips it.
  const auto bom = read(std::string("\x1c\0\xef\xbb\xbf", 5) + "eta charset=\"unknown-charset\"><p>x</p>",
                        "text/html; charset=utf-8");
  REQUIRE(bom.text == std::string("\xef\xbb\xbf", 3) + "eta charset=\"unknown-charset\">\nx");
  REQUIRE(QString::fromUtf8(bom.text.data(), qsizetype(bom.text.size())).toUtf8().toStdString() != bom.text);
  QStringDecoder decoder(QStringDecoder::Utf8,
                         QStringConverter::Flag::ConvertInitialBom | QStringConverter::Flag::Stateless);
  const QString preserved = decoder(QByteArrayView(bom.text.data(), qsizetype(bom.text.size())));
  REQUIRE(!decoder.hasError());
  REQUIRE(preserved.front().unicode() == 0xfeff);
  REQUIRE(preserved.toUtf8().toStdString() == bom.text);
  QStringDecoder invalid(QStringDecoder::Utf8,
                         QStringConverter::Flag::ConvertInitialBom | QStringConverter::Flag::Stateless);
  const QString truncated = invalid(QByteArrayView("\xf0\x9f", 2));
  REQUIRE(invalid.hasError());
  REQUIRE(!truncated.isEmpty());
  REQUIRE(read("<table><tr><td>New</td><td>York</td></tr></table>").text == "New\nYork");
  REQUIRE(read("<base href='https://cdn.example.org/docs/'><a href='guide'>x</a>").links[0]["url"] ==
          "https://cdn.example.org/docs/guide");
  REQUIRE(read("<base href='javascript:invalid'><base href='https://other.example/'><a href='guide'>x</a>")
              .links[0]["url"] == "https://example.com/guide");
  REQUIRE(read("<a href='https://@example.com/'>x</a>").links.empty());
  REQUIRE(read("<base href='https://@other.example/'><a href='guide'>x</a>").links[0]["url"] ==
          "https://example.com/guide");
  REQUIRE(read("<!--<meta charset=GBK>--><script>'<meta charset=GBK>'</script><p>中文</p>").text == "中文");
  REQUIRE(read("<meta charset=UTF-8><p>" + std::string("\xd6\xd0\xce\xc4", 4), "text/html; charset=\"GBK\"")
              .text == "中文");
  REQUIRE(read(std::string("\xff\xfe\x2d\x4e", 4), "text/plain; charset=UTF-8").text == "中");
  auto big5 = read(std::string(4095, 'a') + std::string("\x88\x62", 2) + "Z", "text/plain; charset=Big5");
  REQUIRE(QString::fromStdString(big5.text).remove('\n').endsWith(QString::fromUtf8("Ê̄Z")));
  auto cut = read("<p>ok" + std::string(1024 * 1024 - 6, ' ') + "😀");
  REQUIRE(cut.truncated);
  REQUIRE(cut.text == "ok");
  auto capped = read(QString::fromUtf8("😀").repeated(200001).toStdString(), "text/plain");
  REQUIRE(capped.truncated);
  REQUIRE(QString::fromStdString(capped.text).toUcs4().size() == 200000);
  std::string exactLines;
  for (int i = 0; i < 10000; ++i)
    exactLines += "<p>x</p>";
  auto exact = read(exactLines);
  auto over = read(exactLines + "<p>y</p>");
  REQUIRE(!exact.truncated);
  REQUIRE(QString::fromStdString(exact.text).split('\n').size() == 10000);
  REQUIRE(over.truncated);
  REQUIRE(over.text == exact.text);
  std::string hiddenDeep = "<base href='https://example.com/'><div hidden>";
  for (int i = 0; i < 514; ++i)
    hiddenDeep += "<div>";
  error("response_too_large", [&] { read(hiddenDeep + "x"); });
}
void serviceTests() {
  QTemporaryDir dir;
  auto t = transport();
  std::atomic_int searches = 0;
  std::string form;
  t.search = [&](auto, auto body, auto, auto) {
    ++searches;
    form = body;
    return results();
  };
  Service s(config(dir), t);
  auto r = s.call("web_search", {{"query", "C++ 100%25 中文 a&b"}, {"limit", 1}}, "alice", token());
  REQUIRE(r["status"] == "ok");
  QUrlQuery q(QString::fromStdString(form));
  REQUIRE(q.queryItemValue("q", QUrl::FullyDecoded) == "C++ 100%25 中文 a&b");
  r = s.call("web_search", {{"query", "C++ 100%25 中文 a&b"}, {"limit", 1}}, "alice", token());
  REQUIRE(r["cache"]["hit"] == true);
  REQUIRE(searches == 1);
  s.call("web_search", {{"query", "C++ 100%25 中文 a&b"}, {"limit", 1}}, "bob", token());
  REQUIRE(searches == 2);
  r = s.call("web_search", {{"query", "q"}, {"domains", Json::array({"EXAMPLE.COM."})}}, "alice", token());
  REQUIRE(r["results"].size() == 1);
  r = s.call("web_search", {{"query", "q"}, {"domains", Json::array({"other.example"})}}, "alice", token());
  REQUIRE(r["results"].empty());
  std::string largeQuery;
  for (int i = 0; i < 2048; ++i)
    largeQuery += "😀";
  REQUIRE(s.call("web_search", {{"query", largeQuery}}, "alice", token())["results"].size() == 1);
  auto p = s.call("web_open", {{"url", "https://example.com/a"}, {"max_lines", 1}}, "alice", token());
  REQUIRE(p["lines"].size() == 1);
  REQUIRE(p["next_start_line"] == 2);
  const auto id = p["page_id"];
  REQUIRE(s.call("web_open", {{"page_id", id}, {"start_line", 2}}, "alice", token())["lines"][0]["line"] ==
          2);
  auto f = s.call("web_find", {{"page_id", id}, {"pattern", "hello"}, {"max_matches", 1}}, "alice", token());
  REQUIRE(f["matches"].size() == 1);
  REQUIRE(f["next_start_line"] == 2);
  f = s.call("web_find", {{"page_id", id}, {"pattern", "hello"}, {"start_line", 2}}, "alice", token());
  REQUIRE(f["matches"][0]["line"] == 2);
  error("page_unavailable",
        [&] { s.call("web_find", {{"page_id", id}, {"pattern", "hello"}}, "bob", token()); });
  error("invalid_argument",
        [&] { s.call("web_open", {{"url", "https://example.com"}, {"page_id", id}}, "alice", token()); });
  REQUIRE(s.status()["active_calls"] == 0);
}
void unicodePageTests() {
  QTemporaryDir dir;
  auto t = transport();
  int requests = 0;
  const std::string marker("\xef\xbb\xbf", 3);
  t.open = [&](auto url, auto) {
    ++requests;
    return HttpResponse{
        200, std::string("\x1c\0", 2) + marker + "word<p>word</p>", "text/html; charset=utf-8", url, {}};
  };
  Service service(config(dir), t);
  auto opened = service.call("web_open", {{"url", "https://example.com/bom"}}, "alice", token());
  REQUIRE(opened["lines"][0]["text"] == marker + "word");
  const auto id = opened["page_id"];
  auto cached = service.call("web_open", {{"page_id", id}}, "alice", token());
  REQUIRE(cached["lines"] == opened["lines"]);
  REQUIRE(requests == 1);
  auto found = service.call("web_find", {{"page_id", id}, {"pattern", marker}}, "alice", token());
  REQUIRE(found["matches"].size() == 1);
  REQUIRE(found["matches"][0]["line"] == 1);
  REQUIRE(found["matches"][0]["text"] == marker + "word");
  found = service.call("web_find", {{"page_id", id}, {"pattern", marker + "WORD"}}, "alice", token());
  REQUIRE(found["matches"].size() == 1);
  found = service.call("web_find", {{"page_id", id}, {"pattern", marker + "WORD"}, {"case_sensitive", true}},
                       "alice", token());
  REQUIRE(found["matches"].empty());
  for (const auto &body : {std::string("<p>&#xfeff;word</p>"), "<p>" + marker + "word</p>",
                           "<p><span>" + marker + "word</span></p>"}) {
    QTemporaryDir fragmentDir;
    auto fragmentTransport = transport();
    fragmentTransport.open = [&](auto url, auto) {
      return HttpResponse{200, body, "text/html; charset=utf-8", url, {}};
    };
    Service fragmentService(config(fragmentDir), fragmentTransport);
    auto fragment =
        fragmentService.call("web_open", {{"url", "https://example.com/fragment"}}, "alice", token());
    REQUIRE(fragment["lines"][0]["text"] == marker + "word");
    auto read = fragmentService.call("web_open", {{"page_id", fragment["page_id"]}}, "alice", token());
    REQUIRE(read["lines"] == fragment["lines"]);
    auto match = fragmentService.call("web_find", {{"page_id", fragment["page_id"]}, {"pattern", marker}},
                                      "alice", token());
    REQUIRE(match["matches"].size() == 1);
  }
}
void concurrencyTests() {
  QTemporaryDir dir;
  auto t = transport();
  std::latch entered(5);
  std::atomic_bool release = false;
  t.search = [&](auto, auto, auto, auto c) {
    if (!release)
      entered.count_down();
    while (!release && !c->stopped())
      std::this_thread::sleep_for(std::chrono::milliseconds(5));
    return results();
  };
  Service s(config(dir), t);
  std::vector<std::future<Json>> work;
  for (int i = 0; i < 5; ++i)
    work.push_back(std::async(std::launch::async, [&, i] {
      return s.call("web_search", {{"query", std::to_string(i)}, {"limit", 1}}, "alice", token());
    }));
  entered.wait();
  const auto start = Clock::now();
  error("busy", [&] { s.call("web_search", {{"query", "six"}}, "alice", token()); });
  REQUIRE(Clock::now() - start < std::chrono::milliseconds(200));
  REQUIRE(s.status()["active_calls"] == 5);
  release = true;
  for (auto &f : work)
    REQUIRE(f.get()["results"].size() == 1);
  REQUIRE(s.call("web_search", {{"query", "after"}}, "alice", token())["status"] == "ok");
  REQUIRE(s.status()["active_calls"] == 0);
}
void protocolTests() {
  QTemporaryDir dir;
  auto wide = transport();
  wide.open = [](auto url, auto) {
    std::string body;
    for (int i = 0; i < 200; ++i)
      body += std::string(800, '"') + '\n';
    return HttpResponse{200, body, "text/plain; charset=utf-8", url, {}};
  };
  Service s(config(dir), wide);
  Protocol p(s);
  Json init = {{"jsonrpc", "2.0"},
               {"id", "1"},
               {"method", "initialize"},
               {"params",
                {{"protocolVersion", "2025-06-18"},
                 {"capabilities", Json::object()},
                 {"clientInfo", {{"name", "test"}, {"version", "1"}}}}}};
  auto r = p.handle("alice", "", init);
  REQUIRE(r.httpStatus == 200);
  REQUIRE(!r.sessionId.empty());
  const auto sid = r.sessionId;
  r = p.handle("alice", sid, {{"jsonrpc", "2.0"}, {"id", 1}, {"method", "tools/list"}});
  REQUIRE(r.message->contains("error"));
  r = p.handle("alice", sid, {{"jsonrpc", "2.0"}, {"method", "notifications/initialized"}});
  REQUIRE(r.httpStatus == 202);
  REQUIRE(!r.message);
  r = p.handle("alice", sid, {{"jsonrpc", "2.0"}, {"id", 2}, {"method", "tools/list"}});
  REQUIRE((*r.message)["result"]["tools"].size() == 4);
  r = p.handle("bob", sid, {{"jsonrpc", "2.0"}, {"id", 3}, {"method", "tools/list"}});
  REQUIRE(r.httpStatus == 404);
  r = p.handle(
      "alice", sid,
      {{"jsonrpc", "2.0"},
       {"id", 3},
       {"method", "tools/call"},
       {"params", {{"name", "web_find"}, {"arguments", {{"page_id", "missing"}, {"pattern", "x"}}}}}});
  REQUIRE((*r.message)["result"]["isError"] == true);
  REQUIRE((*r.message)["result"]["structuredContent"]["error"]["code"] == "page_unavailable");
  REQUIRE(p.deleteSession("alice", sid).httpStatus == 204);
  REQUIRE(p.deleteSession("alice", sid).httpStatus == 404);
  REQUIRE(Protocol::validateJsonRpc(Json::array()).has_value());
  REQUIRE(Protocol::validateJsonRpc({{"jsonrpc", "2.0"}, {"method", "ping"}, {"id", true}}).has_value());
  for (const auto &unit :
       {std::string("x"), std::string("\""), std::string("\\"), std::string("\n"), std::string("中")}) {
    std::string large;
    const auto repetitions = (65536 - 2) / (Json(unit).dump().size() - 2);
    for (std::size_t i = 0; i < repetitions; ++i)
      large += unit;
    init["id"] = large;
    r = p.handle("alice", "", init);
    REQUIRE(r.message->contains("result"));
    REQUIRE((*r.message)["id"] == large);
    REQUIRE(p.deleteSession("alice", r.sessionId).httpStatus == 204);
    init["id"] = large + unit;
    r = p.handle("alice", "", init);
    REQUIRE(r.message->contains("error"));
    REQUIRE((*r.message)["id"].is_null());
    REQUIRE(r.sessionId.empty());
  }
  init["id"] = "";
  init["id"] = std::string(1024 * 1024 - init.dump().size(), 'x');
  REQUIRE(init.dump().size() == 1024 * 1024);
  for (const auto &method : {Json("initialize"), Json(17)}) {
    init["method"] = method;
    r = p.handle("alice", "", init);
    REQUIRE(r.message->dump().size() < 1024);
    REQUIRE((*r.message)["error"]["code"] == -32600);
    REQUIRE(r.sessionId.empty());
  }
  // A valid client response is consumed without replying, even beyond our ID budget.
  r = p.handle("alice", "",
               {{"jsonrpc", "2.0"}, {"id", std::string(65536, 'x')}, {"result", Json::object()}});
  REQUIRE(!r.message);
  REQUIRE(r.httpStatus == 400);
  init["id"] = 7;
  init["method"] = "initialize";
  Protocol oversizedVersion(s, std::string(1024 * 1024, 'v'));
  r = oversizedVersion.handle("alice", "", init);
  REQUIRE(r.message->dump().size() < 1024);
  REQUIRE((*r.message)["id"] == 7);
  REQUIRE((*r.message)["error"]["code"] == -32603);
  REQUIRE(r.sessionId.empty());
  r = p.handle("alice", "", init);
  const auto largeSid = r.sessionId;
  const Json largeResponse = {{"jsonrpc", "2.0"}, {"id", std::string(65536, 'x')}, {"result", Json::object()}};
  REQUIRE(p.handle("alice", "unknown", largeResponse).httpStatus == 404);
  REQUIRE(p.handle("bob", largeSid, largeResponse).httpStatus == 404);
  REQUIRE(p.handle("alice", largeSid, largeResponse).httpStatus == 202);
  p.handle("alice", largeSid, {{"jsonrpc", "2.0"}, {"method", "notifications/initialized"}});
  const auto largeId = std::string(65534, 'x');
  r = p.handle("alice", largeSid,
               {{"jsonrpc", "2.0"}, {"id", largeId}, {"method", "tools/call"},
                {"params", {{"name", "web_open"}, {"arguments", {{"url", "https://example.com/wide"}}}}}});
  REQUIRE((*r.message)["id"] == largeId);
  REQUIRE((*r.message)["result"]["isError"] == false);
  REQUIRE((*r.message)["result"]["structuredContent"].dump().size() > 200 * 1024);
  REQUIRE(r.message->dump().size() < 1024 * 1024);
}
void jsonAndDeadlineTests() {
  REQUIRE(!parseBoundedJson(std::string(64, '[') + "0" + std::string(64, ']')).is_discarded());
  for (int depth : {65, 1000, 10000, 30000})
    REQUIRE(parseBoundedJson(std::string(depth, '[') + "0" + std::string(depth, ']')).is_discarded());
  const Json quoted = {{"quoted", std::string(200, '[') + "\\\""}};
  REQUIRE(parseBoundedJson(quoted.dump()) == quoted);
  REQUIRE(parseBoundedJson("{]}").is_discarded());
  for (const int seconds : {20, 60}) {
    QTemporaryDir dir;
    auto c = config(dir);
    c.value["limits"]["open_timeout_seconds"] = seconds;
    std::chrono::milliseconds remaining{};
    auto t = transport();
    const auto original = t.open;
    t.open = [&](auto url, auto cancellation) {
      remaining =
          std::chrono::duration_cast<std::chrono::milliseconds>(cancellation->deadline - Clock::now());
      return original(url, cancellation);
    };
    Service service(c, t);
    Protocol p(service, QCWS_VERSION, {}, Protocol::TransportPolicy::Stdio);
    const auto sid = p.handle("alice", "",
                              {{"jsonrpc", "2.0"},
                               {"id", 1},
                               {"method", "initialize"},
                               {"params",
                                {{"protocolVersion", "2025-06-18"},
                                 {"capabilities", Json::object()},
                                 {"clientInfo", {{"name", "deadline"}, {"version", "1"}}}}}})
                         .sessionId;
    p.handle("alice", sid, {{"jsonrpc", "2.0"}, {"method", "notifications/initialized"}});
    auto call = Json{{"jsonrpc", "2.0"},
                     {"id", 2},
                     {"method", "tools/call"},
                     {"params", {{"name", "web_open"}, {"arguments", {{"url", "https://example.com"}}}}}};
    auto reply = p.handle("alice", sid, call);
    REQUIRE((*reply.message)["result"]["isError"] == false);
    REQUIRE(remaining.count() <= seconds * 1000 && remaining.count() > seconds * 1000 - 1000);
    call["id"] = 3;
    reply = p.handle("alice", sid, call, token(500));
    REQUIRE((*reply.message)["result"]["isError"] == false);
    REQUIRE(remaining.count() > 0 && remaining.count() <= 500);
  }
}
void httpTests() {
  qputenv("QCWS_TEST_CREDENTIAL", QByteArray(64, 'a'));
  QTemporaryDir dir;
  Service service(config(dir), transport());
  HttpServer server(service, config(dir));
  server.start();
  httplib::Client client("127.0.0.1", server.businessPort());
  client.set_connection_timeout(2);
  client.set_read_timeout(3);
  httplib::Headers headers = {{"Host", "test.local"}, {"Authorization", "Bearer " + std::string(64, 'a')}};
  auto r = client.Get("/v1/status", headers);
  REQUIRE(r && r->status == 200);
  REQUIRE(Json::parse(r->body)["contract_version"] == 1);
  r = client.Get("/v1/status", httplib::Headers{{"Host", "test.local"}});
  REQUIRE(r && r->status == 401);
  auto evil = headers;
  evil.emplace("Origin", "https://evil.example");
  r = client.Get("/v1/status", evil);
  REQUIRE(r && r->status == 403);
  r = client.Options("/mcp", {{"Host", "test.local"},
                              {"Origin", "https://client.example"},
                              {"Access-Control-Request-Method", "POST"}});
  REQUIRE(r && r->status == 204);
  r = client.Post("/v1/search", headers, "{\"query\":\"hello\"}", "application/json");
  REQUIRE(r && r->status == 200);
  r = client.Get("/mcp", headers);
  REQUIRE(r && r->status == 405);
  r = client.Post("/mcp", headers, "{}", "application/json");
  REQUIRE(r && r->status == 406);
  auto mcpHeaders = headers;
  mcpHeaders.emplace("Accept", "application/json, text/event-stream");
  Json init = {{"jsonrpc", "2.0"}, {"id", ""}, {"method", "initialize"},
               {"params", {{"protocolVersion", "2025-06-18"}, {"capabilities", Json::object()},
                           {"clientInfo", {{"name", "test"}, {"version", "1"}}}}}};
  init["id"] = std::string(1024 * 1024 - init.dump().size(), 'x');
  r = client.Post("/mcp", mcpHeaders, init.dump(), "application/json");
  REQUIRE(r && r->status == 400);
  REQUIRE(Json::parse(r->body)["error"]["code"] == -32600);
  REQUIRE(!r->has_header("Mcp-Session-Id"));
  init["id"] = 1;
  r = client.Post("/mcp", mcpHeaders, init.dump(), "application/json");
  REQUIRE(r && r->status == 200);
  REQUIRE(Json::parse(r->body)["result"].contains("protocolVersion"));
  httplib::Client control("127.0.0.1", server.controlPort());
  r = control.Get("/health/live", headers);
  REQUIRE(r && r->status == 200);
  server.stop();
  qunsetenv("QCWS_TEST_CREDENTIAL");
}
} // namespace
int main(int argc, char **argv) {
  QCoreApplication app(argc, argv);
  if (argc == 4 &&
      (std::string(argv[1]) == "--tls-timeout-probe" || std::string(argv[1]) == "--tls-cancel-probe")) {
    const bool cancel = std::string(argv[1]) == "--tls-cancel-probe";
    auto cancellation = token(cancel ? 3000 : 300);
    std::jthread canceller([cancel, cancellation] {
      if (cancel) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        cancellation->cancelled = true;
      }
    });
    try {
      HttpClient client(argv[3]);
      client.requestBackend(argv[2], "q=tls", "", cancellation);
      return 41;
    } catch (const Error &e) {
      return e.code == (cancel ? "cancelled" : "timeout") ? 0 : 42;
    }
  }
  if (argc == 4 && std::string(argv[1]) == "--cache-lock-probe") {
    const bool expectOpen = std::string(argv[3]) == "open";
    try {
      Cache cache(argv[2], 16 * 1024 * 1024, 100);
      return expectOpen ? 0 : 31;
    } catch (const Error &e) {
      return !expectOpen && e.code == "cache_unavailable" ? 0 : 32;
    }
  }
  if (argc == 5 && std::string(argv[1]) == "--tls-probe") {
    const bool success = std::string(argv[4]) == "success";
    try {
      HttpClient client(argv[3]);
      auto r = client.requestBackend(argv[2], "q=tls", "", token(5000));
      return success && r.status == 200 && r.body == "q=tls" ? 0 : 21;
    } catch (const Error &e) {
      std::cerr << e.code << '\n';
      return !success && e.code == "provider_unavailable" ? 0 : 22;
    }
  }
  const std::pair<const char *, void (*)()> tests[] = {
      {"network", networkTests},
      {"network-regression", networkRegressionTests},
      {"coalescing", coalescingTests},
      {"cache", cacheTests},
      {"cache-failures", cacheFailureTests},
      {"cache-physical-pressure", cachePhysicalPressureTests},
      {"html", htmlTests},
      {"html-boundaries", htmlBoundaryTests},
      {"service", serviceTests},
      {"unicode-page", unicodePageTests},
      {"concurrency", concurrencyTests},
      {"protocol", protocolTests},
      {"json-and-deadlines", jsonAndDeadlineTests},
      {"http", httpTests}};
  int failed = 0;
  for (auto [name, fn] : tests)
    try {
      std::cout << "RUN " << name << std::endl;
      const auto began = Clock::now();
      fn();
      std::cout << "PASS " << name << " ("
                << std::chrono::duration<double>(Clock::now() - began).count() << " s)" << std::endl;
    } catch (const std::exception &e) {
      ++failed;
      std::cerr << "FAIL " << name << ": " << e.what() << '\n';
    }
  std::cout << assertions << " assertions, " << failed << " failed suites\n";
  return failed ? 1 : 0;
}
