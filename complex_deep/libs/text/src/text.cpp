#include "text/text.hpp"
#include "base/base.hpp"
#include <fmt/core.h>
namespace text {
std::string banner(const std::string& msg) {
  return fmt::format("[{}] {}", base::seed(), msg);
}
}
