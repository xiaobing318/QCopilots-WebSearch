#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <qws/protocol.hpp>
#include <string_view>

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t *data, std::size_t size) {
  if (size > 1048576)
    return 0;
  const auto message = qws::parseBoundedJson(std::string_view(reinterpret_cast<const char *>(data), size));
  if (message.is_discarded())
    return 0;
  const auto validation = qws::Protocol::validateJsonRpc(message);
  const auto encoded = message.dump();
  const auto reparsed = qws::parseBoundedJson(encoded);
  if (reparsed.is_discarded() || reparsed.dump() != encoded ||
      qws::Protocol::validateJsonRpc(reparsed) != validation)
    std::abort();
  return 0;
}
