#pragma once
#include <string>

namespace core {
// Sequence number formatting, shared by everything in the tree.
std::string tag(int n);
// Defined in src/util.cpp -- same basename as src/detail/util.cpp.
int width_of(int n);
// Defined in src/detail/util.cpp.
namespace detail { int digits(int n); }
}  // namespace core
