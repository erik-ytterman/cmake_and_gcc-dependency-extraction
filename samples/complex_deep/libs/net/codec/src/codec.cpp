#include "net/codec/codec.hpp"
namespace net::codec {
unsigned checksum(const char* p, unsigned n) {
  unsigned s = 0;
  for (unsigned i = 0; i < n; ++i) s = s * 31u + static_cast<unsigned>(p[i]);
  return s;
}
}
