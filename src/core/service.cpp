#include <QCryptographicHash>
#include <QDateTime>
#include <QStringDecoder>
#include <QStringList>
#include <QTimeZone>
#include <QUrl>
#include <QUrlQuery>
#include <QUuid>
#include <algorithm>
#include <qws/html.hpp>
#include <qws/service.hpp>
#include <set>
namespace qws {
namespace {
constexpr std::size_t PayloadLimit = 240 * 1024;
QString contentText(const std::string &value) {
  // These bytes are Unicode content from JSON or the extractor, not a new
  // encoded file. An initial U+FEFF must remain part of text and find patterns.
  QStringDecoder decoder(QStringDecoder::Utf8, QStringConverter::Flag::ConvertInitialBom |
                                                  QStringConverter::Flag::Stateless);
  return decoder(QByteArrayView(value.data(), qsizetype(value.size())));
}
std::string now() {
  return QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs).toStdString();
}
void check(const std::shared_ptr<CancelToken> &t) {
  if (t->cancelled.load())
    throw Error("cancelled", "Request cancelled.", 499);
  if (Clock::now() >= t->deadline)
    throw Error("timeout", "Request deadline exceeded.", 504, true);
}
struct Permit {
  std::atomic_int &count;
  Permit(std::atomic_int &c, int max) : count(c) {
    int value = count.load();
    do {
      if (value >= max)
        throw Error("busy", "Business capacity is exhausted.", 429, true);
    } while (!count.compare_exchange_weak(value, value + 1));
  }
  ~Permit() {
    --count;
  }
};
// Partial results are cached briefly so a recovering engine is retried soon.
constexpr std::int64_t PartialTtlSeconds = 30;
// Lifetime chosen by fetchSearch for a search payload. Read-only access.
std::int64_t searchTtl(const Json &payload) {
  const auto cache = payload.find("cache");
  return cache != payload.end() && cache->is_object() ? cache->value("ttl_seconds", std::int64_t(0))
                                                      : std::int64_t(0);
}
QString clip(QString s, int chars) {
  s.truncate(chars);
  if (!s.isEmpty() && s.back().isHighSurrogate())
    s.chop(1);
  return s;
}
std::string bounded(const Json &item, const char *name, int chars) {
  const auto it = item.find(name);
  if (it == item.end() || !it->is_string())
    return {};
  return clip(QString::fromStdString(it->get_ref<const std::string &>()), chars).toStdString();
}
// Up to five distinct, whitespace-normalized strings from an upstream list.
void collectStrings(const Json &data, const char *name, Json &out, std::set<std::string> &seen) {
  const auto it = data.find(name);
  if (it == data.end() || !it->is_array())
    return;
  for (const auto &value : *it) {
    if (out.size() >= 5)
      return;
    if (!value.is_string())
      continue;
    auto text = clip(QString::fromStdString(value.get_ref<const std::string &>()).simplified(), 200).toStdString();
    if (!text.empty() && seen.insert(text).second)
      out.push_back(std::move(text));
  }
}
// SearXNG serializes Python datetimes with isoformat(), with or without an
// offset. Report UTC ISO-8601 and treat values without an offset as UTC.
Json publishedAt(const Json &item) {
  const auto it = item.find("publishedDate");
  if (it == item.end() || !it->is_string())
    return nullptr;
  const auto text = QString::fromStdString(it->get_ref<const std::string &>()).trimmed();
  if (text.isEmpty() || text.size() > 64)
    return nullptr;
  auto date = QDateTime::fromString(text, Qt::ISODate);
  if (!date.isValid())
    return nullptr;
  if (date.timeRepresentation().timeSpec() == Qt::LocalTime)
    date = QDateTime(date.date(), date.time(), QTimeZone::UTC);
  return date.toUTC().toString(Qt::ISODate).toStdString();
}
// Duplicate identity across upstream pages. SearXNG merges duplicates only
// within one response, ignoring www., a trailing slash and http versus https.
// Tracking parameters and default ports are ignored as well. The first URL wins.
std::string dedupKey(const QUrl &url) {
  QUrl base = url.adjusted(QUrl::RemoveScheme | QUrl::RemoveFragment | QUrl::RemoveQuery |
                           QUrl::StripTrailingSlash);
  if (base.port() == 80 || base.port() == 443)
    base.setPort(-1);
  QString key = base.toString(QUrl::FullyEncoded);
  if (key.endsWith('/')) // StripTrailingSlash keeps a root path "/"
    key.chop(1);
  if (key.startsWith(QLatin1String("//www.")))
    key.remove(2, 4);
  QStringList kept;
  for (const auto &part : url.query(QUrl::FullyEncoded).split('&', Qt::SkipEmptyParts)) {
    const auto name = part.section('=', 0, 0).toLower();
    if (!name.startsWith(QLatin1String("utm_")) && name != QLatin1String("gclid") &&
        name != QLatin1String("fbclid"))
      kept.push_back(part);
  }
  if (!kept.isEmpty())
    key += '?' + kept.join('&');
  return key.toStdString();
}
void fit(Json &payload) {
  if (payload.dump().size() <= PayloadLimit)
    return;
  payload["truncated"] = true;
  // Serialize once and subtract each removed element. Field changes made while
  // trimming can shrink the payload by a few bytes, so the estimate stops within
  // that slack and the exact loop below removes precisely the same elements as
  // re-serializing after every removal would.
  constexpr std::size_t Slack = 64;
  std::size_t size = payload.dump().size();
  for (const std::string_view field : {"links", "lines", "matches", "results"}) {
    const auto it = payload.find(field);
    if (it == payload.end() || !it->is_array())
      continue;
    Json &items = *it;
    while (size > PayloadLimit + Slack && !items.empty()) {
      const auto removed = items.back().dump().size() + 1;
      size = removed < size ? size - removed : 0;
      if (field == "links")
        payload["links_truncated"] = true;
      else if (field == "lines" || field == "matches")
        payload["next_start_line"] = items.back()["line"];
      items.erase(items.end() - 1);
    }
    if (size <= PayloadLimit + Slack)
      break;
  }
  while (payload.dump().size() > PayloadLimit) {
    payload["truncated"] = true;
    if (payload.contains("links") && !payload["links"].empty()) {
      payload["links"].erase(payload["links"].end() - 1);
      payload["links_truncated"] = true;
    } else if (payload.contains("lines") && !payload["lines"].empty()) {
      payload["next_start_line"] = payload["lines"].back()["line"];
      payload["lines"].erase(payload["lines"].end() - 1);
    } else if (payload.contains("matches") && !payload["matches"].empty()) {
      payload["next_start_line"] = payload["matches"].back()["line"];
      payload["matches"].erase(payload["matches"].end() - 1);
    } else if (payload.contains("results") && !payload["results"].empty()) {
      payload["results"].erase(payload["results"].end() - 1);
    } else
      throw Error("response_too_large", "Result exceeds the response budget.", 502);
  }
}
} // namespace
struct Service::Job {
  std::shared_ptr<CancelToken> token = std::make_shared<CancelToken>();
  std::mutex mutex;
  std::condition_variable cv;
  std::size_t waiters = 0;
  bool done = false; // value and error are immutable once done is set
  Json value;
  std::exception_ptr error;
  std::atomic_bool finished{false}; // the worker has returned and can be joined promptly
  std::thread worker;
};
Service::Service(Config c, ServiceTransport transport, Cache::TimeSource time)
    : config_(std::move(c)), settings_(config_),
      cache_(config_.path(config_.value["cache"]["directory"]) + "/cache-v1.sqlite",
             config_.value["cache"]["max_bytes"], config_.value["cache"]["max_entries"], std::move(time)),
      transport_(std::move(transport)), capacity_(config_.value["limits"]["max_concurrent"]) {
  const Json &settings = config_.value;
  searchTtl_ = settings.at("cache").at("search_ttl_seconds").get<std::int64_t>();
  pageTtl_ = settings.at("cache").at("page_ttl_seconds").get<std::int64_t>();
  searchTimeout_ = std::chrono::seconds(settings.at("limits").at("search_timeout_seconds").get<int>());
  openTimeout_ = std::chrono::seconds(settings.at("limits").at("open_timeout_seconds").get<int>());
  if (!transport_.open)
    transport_.open = [this](std::string u, std::shared_ptr<CancelToken> t) {
      return http_.getPublic(std::move(u), std::move(t));
    };
}
Service::~Service() {
  shutdown();
  for (auto &j : ownedJobs_)
    if (j->worker.joinable())
      j->worker.join();
}
void Service::shutdown() {
  closing_ = true;
  std::vector<std::shared_ptr<Job>> jobs;
  {
    std::lock_guard lock(mutex_);
    for (auto &w : callsTokens_)
      if (auto t = w.lock())
        t->cancelled = true;
    jobs = ownedJobs_;
    for (auto &j : jobs)
      j->token->cancelled = true;
  }
}
void Service::reap() {
  std::vector<std::shared_ptr<Job>> ended;
  {
    std::lock_guard lock(mutex_);
    std::erase_if(callsTokens_, [](const auto &w) { return w.expired(); });
    std::erase_if(ownedJobs_, [&](const auto &j) {
      // A done job may still be writing the cache; joining it would block this call.
      if (j->finished.load()) {
        ended.push_back(j);
        return true;
      }
      return false;
    });
  }
  for (auto &j : ended)
    if (j->worker.joinable())
      j->worker.join();
}
Json Service::tools() const {
  // contracts/v1/tools.json holds the model-facing metadata in a fixed order;
  // each tool's inputSchema is its canonical contracts/v1 schema.
  static const Json result = [] {
    Json list = Json::array();
    for (const auto &tool : toolCatalog()["tools"]) {
      auto entry = tool;
      entry["inputSchema"] = readSchema(tool["name"].get<std::string>());
      list.push_back(std::move(entry));
    }
    return list;
  }();
  return result;
}
Json Service::status() const {
  std::lock_guard lock(mutex_);
  std::size_t waiters = 0;
  for (const auto &[key, job] : jobs_) {
    (void)key;
    std::lock_guard jobLock(job->mutex);
    waiters += job->waiters;
  }
  const auto settings = settings_.current();
  const auto fingerprint = settings_.provider(*settings).fingerprint();
  const auto dependency = fingerprint == dependencyFingerprint_
                              ? dependency_
                              : Json{{"state", "unknown"}, {"last_checked", nullptr}};
  return {{"contract_version", 1},         {"service", closing_ ? "stopping" : "ready"},
          {"provider", dependency},       {"capacity", capacity_},
          {"active_calls", calls_.load()}, {"active_upstream", upstream_.load()},
          {"search_waiters", waiters}, {"settings", settings_.policy(*settings)}};
}
Json Service::call(std::string name, const Json &args, std::string principal,
                   std::shared_ptr<CancelToken> token) {
  if (!token || principal.empty())
    throw Error("unauthorized", "A client identity is required.", 401);
  if (name != "web_search" && name != "web_open" && name != "web_find" && name != "web_search_status")
    throw Error("invalid_argument", "Unknown tool.");
  validateSchema(readSchema(name), args);
  if (name == "web_search_status")
    return status();
  const auto snapshot = settings_.current();
  if ((name == "web_search" && !snapshot->value["tools"]["search"]["enabled"].get<bool>()) ||
      (name == "web_open" && !snapshot->value["tools"]["open"]["enabled"].get<bool>()))
    throw Error("tool_disabled", "The tool is disabled in settings.", 403);
  if (closing_)
    throw Error("backend_unavailable", "Service is stopping.", 503, true);
  Permit permit(calls_, capacity_);
  reap();
  {
    std::lock_guard lock(mutex_);
    callsTokens_.push_back(token);
    if (closing_)
      token->cancelled = true;
  }
  check(token);
  Json result;
  if (name == "web_search")
    result = search(args, principal, token, settings_.provider(*snapshot));
  else if (name == "web_open")
    result = open(args, principal, token);
  else
    result = find(args, principal);
  check(token);
  fit(result);
  return result;
}
std::string Service::key(const std::string &type, const std::string &principal,
                         const std::string &value, const std::string &fingerprint) const {
  return QCryptographicHash::hash(
             QByteArray::fromStdString(Json::array({3, type == "page" ? "page" : fingerprint, type, principal, value}).dump()),
             QCryptographicHash::Sha256)
      .toHex()
      .toStdString();
}
std::string Service::normalizeDomain(const std::string &s) {
  QString domain = QString::fromStdString(s).toLower();
  if (domain.endsWith('.'))
    domain.chop(1);
  const auto ace = QUrl::toAce(domain);
  if (ace.isEmpty() || ace.size() > 253)
    throw Error("invalid_argument", "Invalid domain filter.");
  for (const auto &label : ace.split('.')) {
    if (label.isEmpty() || label.size() > 63 || label.startsWith('-') || label.endsWith('-'))
      throw Error("invalid_argument", "Invalid domain filter.");
    for (unsigned char c : label)
      if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-'))
        throw Error("invalid_argument", "Invalid domain filter.");
  }
  return ace.toStdString();
}
Json Service::search(const Json &args, const std::string &principal,
                     const std::shared_ptr<CancelToken> &token, ProviderConfig provider) {
  // simplified() also collapses inner whitespace, so equivalent queries share
  // one cache entry and one coalesced upstream search.
  Json normalized = {{"query", QString::fromStdString(args["query"].get<std::string>()).simplified().toStdString()},
                     {"limit", args.value("limit", 10)},
                     {"language", args.value("language", "all")},
                     {"time_range", args.value("time_range", "any")},
                     {"domains", Json::array()}};
  if (normalized["query"] == "")
    throw Error("invalid_argument", "Query must contain text.");
  std::set<std::string> domains;
  for (const auto &d : args.value("domains", Json::array()))
    domains.insert(normalizeDomain(d));
  for (const auto &d : domains)
    normalized["domains"].push_back(d);
  const auto cacheKey = key("search", principal, normalized.dump(), provider.fingerprint());
  if (auto value = cache_.get(cacheKey)) {
    (*value)["cache"]["hit"] = true;
    return std::move(*value);
  }
  const auto duration = searchTimeout_;
  token->deadline = std::min(token->deadline, Clock::now() + duration);
  std::shared_ptr<Job> job;
  bool finishedJob = false;
  {
    std::lock_guard lock(mutex_);
    if (auto it = jobs_.find(cacheKey); it != jobs_.end()) {
      const auto &candidate = it->second;
      std::lock_guard guard(candidate->mutex);
      // A finished, cacheable result whose cache write is still in progress is
      // served like a cache hit instead of starting another upstream search.
      finishedJob = candidate->done && !candidate->error && searchTtl(candidate->value) > 0;
      // Otherwise join a running search only while at least half of a fresh
      // search deadline remains.
      if (finishedJob || (!candidate->done && !candidate->token->cancelled &&
                          2 * (candidate->token->deadline - Clock::now()) >= duration)) {
        job = candidate;
        ++job->waiters;
      }
    }
    if (!job) {
      // The network permit survives a waiter's cancellation until its transfer ends.
      auto slot = std::make_shared<Permit>(upstream_, capacity_);
      job = std::make_shared<Job>();
      job->waiters = 1;
      // The upstream stops slightly before its waiters' deadline, so results
      // from earlier pages can still be delivered as partial when a later page
      // runs out of time.
      const auto grace = std::min<Clock::duration>(
          std::chrono::seconds(1), std::chrono::duration_cast<Clock::duration>(duration) / 5);
      job->token->deadline = Clock::now() + duration - grace;
      jobs_[cacheKey] = job;
      ownedJobs_.push_back(job);
      try {
        job->worker = std::thread([this, job, slot, normalized = std::move(normalized), cacheKey,
                                   provider = std::move(provider)]() mutable {
          Json value;
          std::exception_ptr error;
          try {
            value = fetchSearch(normalized, job->token, provider);
            // An expired deadline still returns partial results. Only abandonment
            // by every waiter (or shutdown) discards them.
            if (job->token->cancelled.load())
              throw Error("cancelled", "Request cancelled.", 499);
          } catch (...) {
            error = std::current_exception();
          }
          slot.reset();
          {
            std::lock_guard guard(job->mutex);
            job->value = std::move(value);
            job->error = error;
            job->done = true;
          }
          job->cv.notify_all();
          // Waiters already have the result. The cache write is off their path,
          // and the job stays visible until the write ends so it is not repeated.
          if (!error)
            storeSearch(cacheKey, job->value);
          {
            std::lock_guard lock(mutex_);
            auto it = jobs_.find(cacheKey);
            if (it != jobs_.end() && it->second == job)
              jobs_.erase(it);
          }
          job->finished = true;
        });
      } catch (...) {
        jobs_.erase(cacheKey);
        ownedJobs_.pop_back();
        throw;
      }
    }
  }
  std::unique_lock lock(job->mutex);
  while (!job->done && !token->stopped())
    job->cv.wait_for(lock, std::min<Clock::duration>(std::chrono::milliseconds(50),
                                                     token->deadline - Clock::now()));
  if (--job->waiters == 0 && !job->done)
    job->token->cancelled = true;
  check(token);
  if (job->error)
    std::rethrow_exception(job->error);
  lock.unlock();
  Json value = job->value; // immutable once done
  if (finishedJob)
    value["cache"]["hit"] = true;
  return value;
}
void Service::storeSearch(const std::string &cacheKey, const Json &value) {
  // ttl_seconds is 0 for an uncacheable result (a partial result without hits).
  const auto ttl = searchTtl(value);
  if (ttl <= 0)
    return;
  try {
    cache_.put(cacheKey, value, ttl);
  } catch (...) {
    // The search cache is an optimization. Results already delivered remain
    // valid, and this runs on the worker thread after its error handling.
  }
}
Json Service::fetchSearch(const Json &args, const std::shared_ptr<CancelToken> &token,
                                     const ProviderConfig &config, bool report) {
  auto source = providers_.create(config, http_, transport_.search);
  auto actualProvider = config.id;
  Json warnings = Json::array();
  Json results = Json::array(), failures = Json::array(), corrections = Json::array(),
       suggestions = Json::array();
  // seen holds the duplicate identity of every URL already scanned, including
  // ones a domain filter rejected. reported holds engines already listed.
  std::set<std::string> seen, reported, seenCorrections, seenSuggestions;
  const int limit = args["limit"];
  const auto &domains = args["domains"];
  const auto timeRange = args["time_range"].get<std::string>();
  try {
    const int maxPages = config.id == "searxng" ? 3 : 1;
    for (int pageNumber = 1; pageNumber <= maxPages && int(results.size()) < limit; ++pageNumber) {
      check(token);
      const auto outcome = source->search({args, pageNumber}, token);
      actualProvider = outcome.source;
      const auto &data = outcome.page;
      warnings.insert(warnings.end(), outcome.warnings.begin(), outcome.warnings.end());
      // SearXNG reports [engine, translated reason] pairs. Every name counts
      // toward allFailed; the output lists each engine once across pages.
      std::set<std::string> failed;
      if (const auto unresponsive = data.find("unresponsive_engines");
          unresponsive != data.end() && unresponsive->is_array())
        for (const auto &e : *unresponsive) {
          if (!e.is_array() || e.empty() || !e[0].is_string())
            continue;
          const auto name = clip(QString::fromStdString(e[0].get_ref<const std::string &>()), 100).toStdString();
          failed.insert(name);
          if (!reported.insert(name).second || failures.size() >= 50)
            continue;
          Json failure = {{"engine", name}, {"reason", "unresponsive"}};
          if (e.size() >= 2 && e[1].is_string())
            if (auto detail = clip(QString::fromStdString(e[1].get_ref<const std::string &>()).simplified(), 200);
                !detail.isEmpty())
              failure["detail"] = detail.toStdString();
          failures.push_back(std::move(failure));
        }
      const auto &items = data["results"];
      const bool allFailed = !config.engines.empty() && std::all_of(config.engines.begin(), config.engines.end(), [&](const auto &e) {
                               return failed.contains(e);
                             });
      if (allFailed && items.empty())
        throw Error("provider_unavailable", "All requested search engines failed.", 502, true);
      collectStrings(data, "corrections", corrections, seenCorrections);
      collectStrings(data, "suggestions", suggestions, seenSuggestions);
      if (items.empty())
        break;
      std::size_t fresh = 0;
      for (const auto &item : items) {
        if (!item.is_object())
          continue;
        const auto rawUrl = item.find("url");
        if (rawUrl == item.end() || !rawUrl->is_string())
          continue;
        const auto &raw = rawUrl->get_ref<const std::string &>();
        if (raw.size() > 4096)
          continue;
        QUrl url(QString::fromStdString(raw), QUrl::StrictMode);
        if (!url.isValid() || url.host().isEmpty() || !url.userInfo().isEmpty() ||
            (url.scheme() != "http" && url.scheme() != "https"))
          continue;
        if (!seen.insert(dedupKey(url)).second)
          continue;
        ++fresh;
        if (!domains.empty()) {
          // Strict hostname validation is needed only to match a domain filter.
          std::string host;
          try {
            host = normalizeDomain(url.host().toStdString());
          } catch (const Error &) {
            continue;
          }
          if (std::none_of(domains.begin(), domains.end(), [&](const Json &d) {
                const auto &domain = d.get_ref<const std::string &>();
                return host == domain || host.ends_with("." + domain);
              }))
            continue;
        }
        url.setFragment(QString());
        auto canonical = url.toString(QUrl::FullyEncoded).toStdString();
        if (canonical.size() > 4096)
          continue;
        Json sources = Json::array();
        if (const auto engines = item.find("engines"); engines != item.end() && engines->is_array())
          for (const auto &e : *engines)
            if (e.is_string() && sources.size() < 10)
              sources.push_back(clip(QString::fromStdString(e.get_ref<const std::string &>()), 100).toStdString());
        results.push_back({{"title", bounded(item, "title", 512)},
                           {"url", std::move(canonical)},
                           {"snippet", bounded(item, "content", 2048)},
                           {"sources", std::move(sources)},
                           {"published_at", publishedAt(item)}});
        if (int(results.size()) >= limit)
          break;
      }
      // A page with no URL unseen on earlier pages means the engines are not
      // paginating further. Another full metasearch would only add load.
      if (fresh == 0)
        break;
    }
    if (report)
      reportDependency(config, {{"state", failures.empty() ? "available" : "degraded"}, {"last_checked", now()}});
  } catch (const Error &e) {
    // Cancellation and invalid requests do not indicate provider health.
    if (report && e.code != "cancelled" && e.code != "invalid_argument")
      reportDependency(config, {{"state", results.empty() ? "unavailable" : "degraded"},
                                {"last_checked", now()}, {"error", e.code}});
    if (results.empty())
      throw;
    failures.push_back({{"engine", "provider"}, {"reason", e.code}});
  }
  if (timeRange != "any")
    warnings.push_back("Time range is an upstream hint, not a strict date filter.");
  // Partial results are cached briefly, and not at all without results.
  const bool partial = !failures.empty();
  const std::int64_t ttl = !partial ? searchTtl_ : results.empty() ? 0 : std::min(searchTtl_, PartialTtlSeconds);
  return {{"contract_version", 1},
          {"results", std::move(results)},
          {"provider", actualProvider},
          {"status", partial ? "partial" : "ok"},
          {"failures", std::move(failures)},
          {"warnings", std::move(warnings)},
          {"corrections", std::move(corrections)},
          {"suggestions", std::move(suggestions)},
          {"fetched_at", now()},
          {"cache", {{"hit", false}, {"ttl_seconds", ttl}}}};
}
void Service::reportDependency(const ProviderConfig &config, Json status) {
  std::lock_guard lock(mutex_);
  const auto fingerprint = config.fingerprint();
  if (fingerprint == settings_.provider(*settings_.current()).fingerprint()) {
    dependency_ = std::move(status);
    dependencyFingerprint_ = fingerprint;
  }
}

Json Service::listeningAddresses() const {
  Json listeners = Json::object();
  for (const auto *name : {"business", "control"}) {
    const auto &listener = config_.value.at(name);
    listeners[name] = {{"host", listener.at("host")}, {"port", listener.at("port")}};
  }
  const auto admin = adminConfiguration(config_, false);
  listeners["admin"] = {{"host", admin.host}, {"port", admin.port}};
  return listeners;
}

Json Service::testProvider(const std::string &id) {
  if (id != "auto" && id != "searxng" && id != "brave" && id != "tavily" && id != "exa" &&
      id != "perplexity" && id != "deepseek")
    throw Error("invalid_argument", "Unknown search provider.");
  const auto started = Clock::now();
  auto token = std::make_shared<CancelToken>();
  token->deadline = started + searchTimeout_;
  const auto elapsed = [&] {
    return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - started).count();
  };
  try {
    Permit permit(upstream_, capacity_);
    reap();
    {
      std::lock_guard lock(mutex_);
      if (closing_)
        throw Error("backend_unavailable", "Service is stopping.", 503, true);
      callsTokens_.push_back(token);
    }
    const auto provider = settings_.provider(*settings_.current(), id);
    auto result = fetchSearch({{"query", "QGIS"}, {"limit", 3}, {"language", "all"},
                               {"time_range", "any"}, {"domains", Json::array()}},
                              token, provider, false);
    return {{"ok", true}, {"source", result["provider"]}, {"result_count", result["results"].size()},
            {"elapsed_ms", elapsed()}, {"sample", std::move(result["results"])}};
  } catch (const Error &e) {
    return {{"ok", false}, {"source", id}, {"result_count", 0}, {"elapsed_ms", elapsed()},
            {"sample", Json::array()}, {"error", errorPayload(e)["error"]}};
  }
}
Json Service::page(const std::string &principal, const std::string &id) {
  if (auto p = cache_.get(key("page", principal, id)))
    return std::move(*p);
  throw Error("page_unavailable", "Page is unknown or expired for this client.", 404);
}
Json Service::open(const Json &args, const std::string &principal,
                   const std::shared_ptr<CancelToken> &token) {
  // The input schema carries no oneOf, so the exclusive choice is enforced here.
  if (args.contains("url") == args.contains("page_id"))
    throw Error("invalid_argument", "Supply exactly one of url or page_id.");
  Json document;
  const bool hit = args.contains("page_id");
  if (hit)
    document = page(principal, args["page_id"].get<std::string>());
  else {
    token->deadline = std::min(token->deadline, Clock::now() + openTimeout_);
    Permit upstream(upstream_, capacity_);
    auto response = transport_.open(args["url"].get<std::string>(), token);
    check(token);
    // Matches HttpClient::getPublic: page statuses never use search-provider codes.
    if (response.status < 200 || response.status >= 300)
      throw Error("upstream_http_error", "Remote resource returned HTTP " + std::to_string(response.status) + ".",
                  502, response.status == 429 || response.status >= 500);
    auto parsed = parseHtml(response.body, response.url, response.contentType);
    // The snapshot keeps every extracted link with its 1-based text line.
    document = {{"contract_version", 1},
                {"page_id", QUuid::createUuid().toString(QUuid::WithoutBraces).toStdString()},
                {"url", response.url},
                {"title", std::move(parsed.title)},
                {"text", std::move(parsed.text)},
                {"links", std::move(parsed.links)},
                {"links_truncated", parsed.linksTruncated},
                {"fetched_at", now()},
                {"truncated", parsed.truncated}};
    cache_.put(key("page", principal, document["page_id"].get<std::string>()), document, pageTtl_);
  }
  const auto lines = contentText(document["text"].get<std::string>()).split('\n');
  const int first = args.value("start_line", 1), count = args.value("max_lines", 200);
  Json links = std::move(document["links"]);
  document.erase("text");
  Json selected = Json::array();
  for (int i = first - 1; i < lines.size() && i < first - 1 + count; ++i)
    selected.push_back({{"line", i + 1}, {"text", lines[i].toStdString()}});
  // Return only links whose line is inside the returned window.
  const int returned = int(selected.size()), last = first + returned - 1;
  Json windowLinks = Json::array();
  if (returned > 0 && links.is_array())
    for (auto &link : links) {
      const auto line = link.find("line"); // end() for a non-object
      if (line == link.end() || !line->is_number_integer())
        continue;
      if (const auto number = line->get<std::int64_t>(); number >= first && number <= last)
        windowLinks.push_back(std::move(link));
    }
  document["lines"] = std::move(selected);
  document["links"] = std::move(windowLinks);
  document["links_truncated"] = document.value("links_truncated", false);
  document["total_lines"] = lines.size();
  const int next = first + returned;
  document["next_start_line"] = next <= lines.size() ? Json(next) : Json(nullptr);
  document["cache"] = {{"hit", hit}, {"ttl_seconds", pageTtl_}};
  return document;
}
Json Service::find(const Json &args, const std::string &principal) {
  const auto document = page(principal, args["page_id"]);
  const auto lines = contentText(document["text"].get<std::string>()).split('\n');
  const auto pattern = contentText(args["pattern"].get<std::string>());
  Json matches = Json::array();
  bool truncated = false;
  Json next = nullptr;
  for (int i = args.value("start_line", 1) - 1; i < lines.size(); ++i)
    if (lines[i].contains(pattern,
                          args.value("case_sensitive", false) ? Qt::CaseSensitive : Qt::CaseInsensitive)) {
      if (int(matches.size()) >= args.value("max_matches", 20)) {
        truncated = true;
        next = i + 1;
        break;
      }
      matches.push_back({{"line", i + 1}, {"text", lines[i].toStdString()}});
    }
  return {{"contract_version", 1},
          {"page_id", args["page_id"]},
          {"matches", matches},
          {"truncated", truncated},
          {"next_start_line", next}};
}
} // namespace qws
