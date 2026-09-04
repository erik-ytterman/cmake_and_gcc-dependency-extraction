#include "core/core.hpp"
#include "internal.hpp"

namespace core {
int width_of(int n) { return n < internal::kBase ? 1 : 1 + width_of(n / internal::kBase); }
}  // namespace core
