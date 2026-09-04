#include "jsonio/jsonio.hpp"
#include <cassert>
#include <cmath>
#include <cstdio>
int main() {
  auto s = jsonio::encode("temp", 21.5);
  assert(std::fabs(jsonio::value_of(s) - 21.5) < 1e-9);
  std::puts("jsonio_test passed");
}
