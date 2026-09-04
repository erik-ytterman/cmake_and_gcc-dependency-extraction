#include "textutil/textutil.hpp"
#include <cassert>
#include <cstdio>
int main() {
  auto f = textutil::fields(" a , b ,c ", ',');
  assert(f.size() == 3 && f[0] == "a" && f[2] == "c");
  assert(textutil::join_upper(f).rfind("A|B|C", 0) == 0);
  std::puts("textutil_test passed");
}
