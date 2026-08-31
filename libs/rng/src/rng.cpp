#include "rng/rng.hpp"

#include <chrono>

namespace rng {

Generator make_seeded() {
  std::random_device rd;
  std::uint64_t seed = (static_cast<std::uint64_t>(rd()) << 32) ^ rd() ^
                       static_cast<std::uint64_t>(
                           std::chrono::steady_clock::now().time_since_epoch().count());
  return Generator(seed);
}

}  // namespace rng
