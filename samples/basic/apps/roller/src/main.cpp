#include <fmt/color.h>
#include <fmt/core.h>

#include <cstdlib>

#include "build_info.hpp"
#include "rng/rng.hpp"

// Dice roller: uses rng + fmt but NOT input, giving it a distinct closure.
int main(int argc, char** argv) {
  const int count = (argc > 1) ? std::atoi(argv[1]) : 1;

  fmt::print(fmt::emphasis::bold, "Dice Roller (v{})\n", build_info::version);

  auto gen = rng::make_seeded();
  for (int i = 0; i < count; ++i) {
    fmt::print(fmt::fg(fmt::color::magenta), "  die {}: {}\n", i + 1,
               gen.between(1, 6));
  }
  return 0;
}
