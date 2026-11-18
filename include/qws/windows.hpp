#pragma once
#ifdef _WIN32
#include <QByteArray>
#include <QString>
#include <string>
namespace qws {
// Set once at startup. Runtime JSON cannot relax the credential policy.
void configureWindowsCredentialPolicy(bool installedService, const std::string &serviceName);
QByteArray readWindowsCredential(const QString &path);
void writeWindowsPrivateFile(const QString &path, const QByteArray &contents);
QByteArray readWindowsAdminCredential(const QString &path);
void writeWindowsAdminCredential(const QString &path, const QByteArray &contents);
QByteArray readWindowsServicePrivateFile(const QString &path);
void writeWindowsServicePrivateFile(const QString &path, const QByteArray &contents);
void initializeWindowsTransaction(const QString &path, const QString &transaction);
void finishWindowsInitialization(const QString &path, const QString &transaction, bool rollback);
} // namespace qws
#endif
