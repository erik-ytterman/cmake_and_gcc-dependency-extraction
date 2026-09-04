#include "core/codec/codec.hpp"
#include "core/core.hpp"
#include <sstream>
namespace core::codec {
std::string hex(int n) { std::ostringstream o; o << std::hex << n << "/" << core::width_of(n); return o.str(); }
}  // namespace core::codec
