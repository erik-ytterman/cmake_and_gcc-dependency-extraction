#pragma once
#include <string>
namespace jsonio {
// Render a labelled measurement as a JSON document.
std::string encode(const std::string& label, double value);
// Read the "value" field back out of one.
double value_of(const std::string& json_text);
}  // namespace jsonio
