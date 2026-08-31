#include <fmt/color.h>
#include <fmt/core.h>

#include <iostream>

#include "build_info.hpp"
#include "input/input.hpp"

// Greeter: uses input + fmt but NOT rng, giving it a distinct closure.
int main() {
  fmt::print("Greeter (v{})\n", build_info::version);

  std::cout << fmt::format(fmt::fg(fmt::color::cyan), "What's your name? ");
  auto name = input::read_line(std::cin);
  if (!name || name->empty()) {
    fmt::print("No name given.\n");
    return 0;
  }
  fmt::print(fmt::emphasis::bold | fmt::fg(fmt::color::green), "Hello, {}!\n",
             *name);
  return 0;
}
