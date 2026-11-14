#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFileInfo>
#include <QLockFile>
#include <QMetaObject>
#include <QObject>
#include <QPointer>
#include <QSqlDatabase>
#include <QSqlQuery>
#include <QThread>
#include <QUuid>
#include <QVariant>
#include <future>
#include <limits>
#include <mutex>
#include <qws/cache.hpp>
#include <type_traits>
namespace qws {
namespace {
[[noreturn]] void unavailable() {
  throw Error("cache_unavailable", "Cache is unavailable.", 503, true);
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
    return future.get();
  }
  QSqlQuery query(const QString &sql, const QVariantList &values = {}) {
    QSqlQuery q(db);
    if (!q.prepare(sql))
      unavailable();
    for (const auto &v : values)
      q.addBindValue(v);
    if (!q.exec())
      unavailable();
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
    if (db.isValid())
      db.close();
    db = QSqlDatabase();
    if (QSqlDatabase::contains(name))
      QSqlDatabase::removeDatabase(name);
    lockFile.unlock();
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
  return d_->run([&]() -> std::optional<Json> {
    QByteArray bytes;
    {
      auto q = d_->query("SELECT payload FROM entries WHERE key=? AND expires>?", {k, qint64(d_->clock())});
      if (!q.next()) {
        q.finish();
        d_->query("DELETE FROM entries WHERE key=? AND expires<=?", {k, qint64(d_->clock())});
        return std::nullopt;
      }
      bytes = q.value(0).toByteArray();
    }
    Json value = Json::parse(bytes.constData(), bytes.constData() + bytes.size(), nullptr, false);
    if (value.is_discarded()) {
      d_->query("DELETE FROM entries WHERE key=?", {k});
      return std::nullopt;
    }
    d_->query("UPDATE entries SET accessed=? WHERE key=?", {++d_->serial, k});
    return value;
  });
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
    if (!d_->db.transaction())
      unavailable();
    try {
      d_->query("DELETE FROM entries WHERE expires<=? OR key=?", {now, k});
      while (d_->scalar("SELECT COUNT(*) FROM entries") >= d_->maxEntries ||
             d_->scalar("SELECT COALESCE(SUM(size),0) FROM entries") + data.size() > d_->payloadBudget)
        d_->query("DELETE FROM entries WHERE key=(SELECT key FROM entries ORDER BY accessed,key LIMIT 1)");
      d_->query("INSERT INTO entries VALUES(?,?,?,?,?)",
                {k, data, qint64(data.size()), now + ttl, ++d_->serial});
      if (!d_->db.commit())
        unavailable();
    } catch (...) {
      d_->db.rollback();
      throw;
    }
    if (d_->storageBytes() > d_->maxBytes)
      unavailable();
  });
}
void Cache::erase(const std::string &key) {
  const auto k = keyHash(key);
  d_->run([&] { d_->query("DELETE FROM entries WHERE key=?", {k}); });
}
Json Cache::stats() {
  return d_->run([&] {
    d_->query("DELETE FROM entries WHERE expires<=?", {qint64(d_->clock())});
    return Json{{"entries", d_->scalar("SELECT COUNT(*) FROM entries")},
                {"logical_bytes", d_->scalar("SELECT COALESCE(SUM(size),0) FROM entries")},
                {"storage_bytes", d_->storageBytes()},
                {"max_storage_bytes", d_->maxBytes},
                {"effective_payload_limit", d_->payloadBudget}};
  });
}
} // namespace qws
