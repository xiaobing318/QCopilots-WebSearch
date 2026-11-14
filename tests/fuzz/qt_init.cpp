#include <QCoreApplication>

extern "C" int LLVMFuzzerInitialize(int *, char ***) {
  static int argc = 1;
  static char name[] = "qcws-fuzzer";
  static char *argv[] = {name, nullptr};
  static QCoreApplication application(argc, argv);
  return 0;
}
