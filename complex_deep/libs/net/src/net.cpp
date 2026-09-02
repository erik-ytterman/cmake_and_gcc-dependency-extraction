#include "net/net.hpp"
#include "net/codec/codec.hpp"
#include "base/base.hpp"
#include <thread>
namespace net {
std::string handshake() {
  unsigned c = 0;
  std::thread t([&] { c = codec::checksum("hello", 5) + base::seed(); });
  t.join();
  return std::to_string(c);
}
}
