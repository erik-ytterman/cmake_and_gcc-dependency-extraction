#include <fmt/color.h>
#include <fmt/core.h>

#include <iostream>

#include "build_info.hpp"
#include "input/input.hpp"
#include "rng/rng.hpp"

int main() {
  fmt::print(fmt::emphasis::bold | fmt::fg(fmt::color::green),
             "Number Guessing Game  (v{})\n", build_info::version);

  auto gen = rng::make_seeded();
  const int secret = gen.between(1, 100);
  int tries = 0;

  while (true) {
    auto guess = input::read_int(std::cin, std::cout, "Your guess", 1, 100);
    if (!guess) {
      fmt::print("\nBye!\n");
      return 0;
    }
    ++tries;
    if (*guess == secret) {
      fmt::print(fmt::fg(fmt::color::green), "Correct in {} tries!\n", tries);
      return 0;
    }
    fmt::print(fmt::fg(fmt::color::yellow),
               *guess < secret ? "Higher\n" : "Lower\n");
  }
}
