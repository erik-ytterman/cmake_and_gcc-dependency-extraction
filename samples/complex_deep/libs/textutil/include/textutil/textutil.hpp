#pragma once
#include <string>
#include <vector>
namespace textutil {
// Split on a delimiter and trim each field (Boost.StringAlgo).
std::vector<std::string> fields(const std::string& line, char delim);
std::string join_upper(const std::vector<std::string>& parts);
}  // namespace textutil
