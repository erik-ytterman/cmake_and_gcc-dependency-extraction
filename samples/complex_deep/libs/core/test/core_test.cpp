#include "core/core.hpp"
#include <cassert>
#include <cstdio>
int main() {
  assert(core::width_of(5) == 1);
  assert(core::width_of(12345) == 5);
  assert(core::detail::digits(0) == 1);
  assert(!core::tag(3).empty());
  std::puts("core_test passed");
}
