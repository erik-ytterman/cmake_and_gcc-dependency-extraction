#include "base/base.hpp"
#include "complex_deep/version.hpp"
namespace base {
int seed() { return 1 + (complex_deep::kVersion[0] != '\0'); }
}
