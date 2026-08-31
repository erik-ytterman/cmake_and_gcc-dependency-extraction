#include "rng/rng.hpp"

#include <cassert>
#include <cstdio>

int main() {
  // Values stay within the requested inclusive range.
  rng::Generator g(12345);
  for (int i = 0; i < 1000; ++i) {
    int v = g.between(1, 6);
    assert(v >= 1 && v <= 6);
  }

  // Same seed => same sequence (determinism).
  rng::Generator a(42), b(42);
  for (int i = 0; i < 100; ++i) {
    assert(a.between(0, 1000000) == b.between(0, 1000000));
  }

  std::puts("rng_test passed");
  return 0;
}
