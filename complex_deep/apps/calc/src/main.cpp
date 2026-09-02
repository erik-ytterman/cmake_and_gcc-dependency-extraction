#include "mathx/mathx.hpp"
#include "complex_deep/version.hpp"
#include <cstdio>
int main() {
  std::printf("calc %s -> %d\n", complex_deep::kVersion, mathx::outer() + mathx::inner());
  return 0;
}
