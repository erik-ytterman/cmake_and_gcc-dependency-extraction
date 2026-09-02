#include "input/input.hpp"

#include <cassert>
#include <cstdio>
#include <sstream>

int main() {
  // Valid input in range.
  {
    std::istringstream in("42\n");
    std::ostringstream out;
    auto v = input::read_int(in, out, "pick", 0, 100);
    assert(v.has_value() && *v == 42);
  }

  // Out of range, then valid: should retry and accept the second value.
  {
    std::istringstream in("999\n7\n");
    std::ostringstream out;
    auto v = input::read_int(in, out, "pick", 0, 10);
    assert(v.has_value() && *v == 7);
  }

  // Non-numeric, then valid.
  {
    std::istringstream in("banana\n3\n");
    std::ostringstream out;
    auto v = input::read_int(in, out, "pick", 0, 10);
    assert(v.has_value() && *v == 3);
  }

  // EOF yields nullopt.
  {
    std::istringstream in("");
    std::ostringstream out;
    auto v = input::read_int(in, out, "pick", 0, 10);
    assert(!v.has_value());
  }

  std::puts("input_test passed");
  return 0;
}
