#include "core/core.hpp"
#include "internal.hpp"
#include <complex_deep/version.hpp>

namespace core {
std::string tag(int n) {
  return std::string(complex_deep::kVersion) + ":" + std::to_string(n * internal::kBase / internal::kBase);
}
}  // namespace core
