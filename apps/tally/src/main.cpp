#include <cstdio>
#include <cstdlib>

#include "build_info.hpp"
#include "rng/rng.hpp"

// Die-roll histogram: uses rng + build_info but NOT input and NOT fmt, so its
// closure contains no third-party dependency at all. Output goes through the
// standard library, which needs nothing declared in the extracted CMakeLists.
int main(int argc, char** argv) {
  const int rolls = (argc > 1) ? std::atoi(argv[1]) : 60;

  std::printf("Tally (v%s)\n", build_info::version);

  int counts[6] = {0};
  auto gen = rng::make_seeded();
  for (int i = 0; i < rolls; ++i) {
    ++counts[gen.between(1, 6) - 1];
  }

  for (int face = 0; face < 6; ++face) {
    std::printf("  %d: %-4d ", face + 1, counts[face]);
    for (int bar = 0; bar < counts[face]; ++bar) {
      std::putchar('#');
    }
    std::putchar('\n');
  }
  return 0;
}
