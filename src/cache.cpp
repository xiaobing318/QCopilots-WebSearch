#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFileInfo>
#include <QLockFile>
#include <QMetaObject>
#include <QObject>
#include <QPointer>
#include <QSqlDatabase>
#include <QSqlError>
#include <QSqlQuery>
#include <QThread>
#include <QUuid>
#include <QVariant>
#include <algorithm>
#include <future>
#include <limits>
#include <map>
#include <mutex>
#include <qws/cache.hpp>
#include <type_traits>
#include <vector>
namespace qws {
namespace {
[[noreturn]] void unavailable() {
  throw Error("cache_unavailable", "Cache is unavailable.", 503, true);
}
struct SqlFull {};
[[noreturn]] void sqlFailure(const QSqlError &error) {
  bool numeric = false;
  const auto code = error.nativeErrorCode().toInt(&numeric);
  if (numeric && (code & 0xff) == 13) // SQLITE_FULL, including extended codes.
    throw SqlFull();
  unavailable();
}
QString keyHash(const std::string &key) {
  if (key.size() > 8192)
    throw Error("invalid_argument", "Cache key is too long.");
  return QString::fromLatin1(
      QCryptographicHash::hash(QByteArray::fromStdString(key), QCryptographicHash::Sha256).toHex());
}
} // namespace
struct Cache::Impl {
  QString path, name;
  qint64 maxBytes, payloadBudget = 0, serial = 0;
  int maxEntries;
  TimeSource clock;
  QThread thread;
  QPointer<QObject> worker;
  QSqlDatabase db;
  QLockFile lockFile;
  std::mutex dispatchMutex;
  std::map<QString, qint64> pendingTouches;
  bool closed = false;
  Impl(std::string p, qint64 bytes, int entries, TimeSource c)
      : path(QString::fromStdString(p)),
        name("qws-cache-" + QUuid::createUuid().toString(QUuid::WithoutBraces)), maxBytes(bytes),
        maxEntries(entries), clock(std::move(c)), lockFile(path + ".lock") {
    if (bytes < 16LL * 1024 * 1024 || bytes > (1LL << 40) || entries < 1 || entries > 1000000)
      throw Error("invalid_argument", "Invalid cache capacity.");
    if (!clock)
      clock = [] { return QDateTime::currentSecsSinceEpoch(); };
    lockFile.setStaleLockTime(0);
  }
  template <class F> auto run(F &&fn) -> std::invoke_result_t<F> {
    if (QThread::currentThread() == &thread)
      unavailable();
    std::lock_guard guard(dispatchMutex);
    if (closed || !worker || !thread.isRunning())
      unavailable();
    using R = std::invoke_result_t<F>;
    std::packaged_task<R()> task(std::forward<F>(fn));
    auto future = task.get_future();
    if (!QMetaObject::invokeMethod(worker, [&task] { task(); }, Qt::BlockingQueuedConnection))
      unavailable();
    try {
      return future.get();
    } catch (const SqlFull &) {
      unavailable();
    }
  }
  QSqlQuery query(const QString &sql, const QVariantList &values = {}) {
    QSqlQuery q(db);
    if (!q.prepare(sql))
      sqlFailure(q.lastError());
    for (const auto &v : values)
      q.addBindValue(v);
    if (!q.exec())
      sqlFailure(q.lastError());
    return q;
  }
  qint64 scalar(const QString &sql, const QVariantList &values = {}) {
    auto q = query(sql, values);
    if (!q.next())
      unavailable();
    return q.value(0).toLongLong();
  }
  qint64 storageBytes() const {
    qint64 size = 0;
    for (const QString &s :
         {QString(), QString("-journal"), QString("-wal"), QString("-shm"), QString(".lock")})
      size += QFileInfo(path + s).size();
    return size;
  }
  void destroyConnection() {
    // Access ordering is expendable metadata. Payload writes remain fully
    // synchronous. A clean shutdown checkpoints hits once, with bounded busy wait.
    if (db.isOpen() && !pendingTouches.empty()) {
      try {
        if (!db.transaction())
          unavailable();
        flushTouches();
        if (!db.commit())
          unavailable();
      } catch (...) {
        db.rollback();
      }
    }
    pendingTouches.clear();
    if (db.isValid())
      db.close();
    db = QSqlDatabase();
    if (QSqlDatabase::contains(name))
      QSqlDatabase::removeDatabase(name);
    lockFile.unlock();
  }
  void flushTouches() {
    if (pendingTouches.empty())
      return;
    // One prepared statement for every pending hit. A completed DML step resets
    // itself, so the statement holds no lock between executions.
    QSqlQuery q(db);
    if (!q.prepare("UPDATE entries SET accessed=? WHERE key=?"))
      sqlFailure(q.lastError());
    for (const auto &[key, accessed] : pendingTouches) {
      q.bindValue(0, accessed);
      q.bindValue(1, key);
      if (!q.exec())
        sqlFailure(q.lastError());
    }
  }
  bool evictPhysical(const QString &protectedKey, qint64 limit) {
    // FULL can roll back the whole transaction, and flushing the access index
    // can itself need another page. Free pages first, using unflushed hit order.
    std::vector<std::pair<qint64, QString>> order;
    auto rows = query("SELECT key,accessed FROM entries WHERE key<>?", {protectedKey});
    while (rows.next()) {
      const auto key = rows.value(0).toString();
      const auto touched = pendingTouches.find(key);
      order.emplace_back(touched == pendingTouches.end() ? rows.value(1).toLongLong() : touched->second, key);
    }
    rows.finish();
    const auto count = std::min(order.size(), std::size_t(limit));
    if (count < order.size())
      std::nth_element(order.begin(), order.begin() + count, order.end());
    QSqlQuery remove(db);
    if (!remove.prepare("DELETE FROM entries WHERE key=?"))
      sqlFailure(remove.lastError());
    for (std::size_t i = 0; i < count; ++i) {
      remove.bindValue(0, order[i].second);
      if (!remove.exec())
        sqlFailure(remove.lastError());
    }
    return count == order.size();
  }
  void start() {
    if (!QDir().mkpath(QFileInfo(path).absolutePath()) || QFileInfo(path).isSymLink())
      unavailable();
    worker = new QObject;
    worker->moveToThread(&thread);
    QObject::connect(&thread, &QThread::finished, worker, &QObject::deleteLater);
    thread.start();
    try {
      run([&] {
        if (!lockFile.tryLock(0) || storageBytes() > maxBytes)
          unavailable();
        db = QSqlDatabase::addDatabase("QSQLITE", name);
        db.setDatabaseName(path);
        db.setConnectOptions("QSQLITE_BUSY_TIMEOUT=250");
        if (!db.open())
          unavailable();
        auto integrity = query("PRAGMA quick_check");
        if (!integrity.next() || integrity.value(0).toString() != "ok")
          unavailable();
        integrity.finish();
        query("PRAGMA page_size=4096");
        query("PRAGMA auto_vacuum=FULL");
        if (scalar("PRAGMA auto_vacuum") != 1)
          unavailable();
        auto mode = query("PRAGMA journal_mode=DELETE");
        if (!mode.next() || mode.value(0).toString().toLower() != "delete")
          unavailable();
        mode.finish();
        query("PRAGMA synchronous=FULL");
        query("PRAGMA temp_store=MEMORY");
        query("PRAGMA cache_size=-2048");
        const qint64 pages = ((maxBytes - 4 * 1024 * 1024) / 3) / scalar("PRAGMA page_size");
        if (scalar(QString("PRAGMA max_page_count=%1").arg(pages)) > pages)
          unavailable();
        payloadBudget = pages * scalar("PRAGMA page_size") * 3 / 4;
        const auto version = scalar("PRAGMA user_version");
        if (version != 0 && version != 1)
          unavailable();
        query("CREATE TABLE IF NOT EXISTS entries(key TEXT PRIMARY KEY,payload BLOB NOT NULL,size INTEGER "
              "NOT NULL CHECK(size>=0),expires INTEGER NOT NULL,accessed INTEGER NOT NULL)");
        query("CREATE INDEX IF NOT EXISTS entry_lru ON entries(accessed,key)");
        query("CREATE INDEX IF NOT EXISTS entry_expiry ON entries(expires)");
        // The payload BLOB precedes size in each row, so totals read through a
        // covering index instead of the table. An existing full database can
        // still open without it; totals then fall back to a table scan.
        try {
          query("CREATE INDEX IF NOT EXISTS entry_size ON entries(size)");
        } catch (const SqlFull &) {
        }
        query("PRAGMA user_version=1");
        serial = scalar("SELECT COALESCE(MAX(accessed),0) FROM entries");
        query("DELETE FROM entries WHERE expires<=?", {qint64(clock())});
      });
    } catch (...) {
      close();
      throw;
    }
  }
  void close() {
    if (QThread::currentThread() == &thread)
      unavailable();
    std::lock_guard guard(dispatchMutex);
    if (closed)
      return;
    closed = true;
    if (worker && thread.isRunning()) {
      QMetaObject::invokeMethod(worker, [this] { destroyConnection(); }, Qt::BlockingQueuedConnection);
      thread.quit();
      thread.wait();
    }
    thread.wait();
    if (worker)
      delete worker.data();
    worker.clear();
  }
};
Cache::Cache(std::string p, std::int64_t bytes, int entries, TimeSource clock)
    : d_(std::make_unique<Impl>(std::move(p), bytes, entries, std::move(clock))) {
  d_->start();
}
Cache::~Cache() {
  close();
}
void Cache::close() {
  if (d_)
    d_->close();
}
std::optional<Json> Cache::get(const std::string &key) {
  const auto k = keyHash(key);
  const auto bytes = d_->run([&]() -> std::optional<QByteArray> {
    auto q = d_->query("SELECT payload FROM entries WHERE key=? AND expires>?", {k, qint64(d_->clock())});
    if (!q.next()) {
      // Expired rows are collected by put. A cache read never needs a disk
      // write merely to update eviction metadata or report a miss.
      d_->pendingTouches.erase(k);
      return std::nullopt;
    }
    auto payload = q.value(0).toByteArray();
    d_->pendingTouches[k] = ++d_->serial;
    return payload;
  });
  if (!bytes)
    return std::nullopt;
  // Parse on the caller's thread so a large snapshot does not hold the single
  // SQLite thread and every other client's cache access.
  Json value = parseBoundedJson(std::string_view(bytes->constData(), size_t(bytes->size())));
  if (!value.is_discarded())
    return value;
  d_->run([&] {
    // Remove only the unreadable bytes, never a valid replacement written since.
    d_->query("DELETE FROM entries WHERE key=? AND payload=?", {k, *bytes});
    d_->pendingTouches.erase(k);
  });
  return std::nullopt;
}
void Cache::put(const std::string &key, const Json &value, std::int64_t ttl) {
  const auto k = keyHash(key);
  const QByteArray data = QByteArray::fromStdString(value.dump());
  d_->run([&] {
    const qint64 now = d_->clock();
    if (ttl <= 0 || ttl > 30LL * 86400 || now > std::numeric_limits<qint64>::max() - ttl)
      throw Error("invalid_argument", "Invalid cache lifetime.");
    if (data.size() > d_->payloadBudget || d_->storageBytes() > d_->maxBytes)
      unavailable();
    qint64 physicalEvictions = 0;
    for (;;) {
      if (!d_->db.transaction())
        unavailable();
      bool allEvicted = false;
      try {
        if (physicalEvictions)
          allEvicted = d_->evictPhysical(k, physicalEvictions);
        // Apply all recent reads in this existing write transaction before eviction.
        // Do not clear on rollback: the next successful write still sees exact LRU.
        d_->flushTouches();
        d_->query("DELETE FROM entries WHERE expires<=? OR key=?", {now, k});
        // Read the totals once, then account for each least-recently-used
        // victim instead of rescanning the table after every deletion.
        qint64 entries = 0, bytes = 0;
        {
          auto totals = d_->query("SELECT COUNT(*),COALESCE(SUM(size),0) FROM entries");
          if (!totals.next())
            unavailable();
          entries = totals.value(0).toLongLong();
          bytes = totals.value(1).toLongLong();
        }
        while (entries >= d_->maxEntries || bytes + data.size() > d_->payloadBudget) {
          QString victim;
          {
            auto oldest = d_->query("SELECT key,size FROM entries ORDER BY accessed,key LIMIT 1");
            if (!oldest.next())
              break;
            victim = oldest.value(0).toString();
            bytes -= oldest.value(1).toLongLong();
          }
          --entries;
          d_->query("DELETE FROM entries WHERE key=?", {victim});
        }
        d_->query("INSERT INTO entries VALUES(?,?,?,?,?)",
                  {k, data, qint64(data.size()), now + ttl, ++d_->serial});
        if (!d_->db.commit())
          sqlFailure(d_->db.lastError());
        d_->pendingTouches.clear();
        break;
      } catch (const SqlFull &) {
        d_->db.rollback();
        // A reopened database can exceed a newly lowered maxEntries setting.
        // Bound retries by the stored population, not the new admission limit.
        const auto candidates = d_->scalar("SELECT COUNT(*) FROM entries WHERE key<>?", {k});
        if (allEvicted || physicalEvictions >= candidates)
          throw;
        physicalEvictions = std::min(candidates, physicalEvictions ? physicalEvictions * 2 : 16);
      } catch (...) {
        d_->db.rollback();
        throw;
      }
    }
    if (d_->storageBytes() > d_->maxBytes)
      unavailable();
  });
}
void Cache::erase(const std::string &key) {
  const auto k = keyHash(key);
  d_->run([&] {
    d_->query("DELETE FROM entries WHERE key=?", {k});
    d_->pendingTouches.erase(k);
  });
}
Json Cache::stats() {
  return d_->run([&] {
    // Read-only: live entries are counted without collecting expired rows.
    const qint64 now = d_->clock();
    return Json{{"entries", d_->scalar("SELECT COUNT(*) FROM entries WHERE expires>?", {now})},
                {"logical_bytes", d_->scalar("SELECT COALESCE(SUM(size),0) FROM entries WHERE expires>?", {now})},
                {"storage_bytes", d_->storageBytes()},
                {"max_storage_bytes", d_->maxBytes},
                {"effective_payload_limit", d_->payloadBudget}};
  });
}
} // namespace qws
