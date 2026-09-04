#include "parsing/parsing.hpp"
#include <cassert>
#include <cstdio>
int main() {
  auto v = parsing::numbers("1, 2.5, 3");
  assert(v.size() == 3 && v[1] == 2.5);
  std::puts("parsing_test passed");
}
