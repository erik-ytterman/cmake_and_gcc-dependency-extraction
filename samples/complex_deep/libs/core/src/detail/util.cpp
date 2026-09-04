#include "core/core.hpp"

namespace core::detail {
int digits(int n) { return n == 0 ? 1 : (n < 0 ? digits(-n) : (n < 10 ? 1 : 1 + digits(n / 10))); }
}  // namespace core::detail
