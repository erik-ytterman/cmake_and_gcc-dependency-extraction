#pragma once

#include <cstdint>
#include <random>

namespace rng {

// Thin wrapper around a deterministic PRNG. Construct with an explicit seed for
// reproducibility (used in tests), or via make_seeded() for real runs.
class Generator {
 public:
  explicit Generator(std::uint64_t seed) : engine_(seed) {}

  // Uniform integer in the inclusive range [lo, hi].
  int between(int lo, int hi) {
    std::uniform_int_distribution<int> dist(lo, hi);
    return dist(engine_);
  }

 private:
  std::mt19937_64 engine_;
};

// Create a Generator seeded from a non-deterministic source.
Generator make_seeded();

}  // namespace rng
