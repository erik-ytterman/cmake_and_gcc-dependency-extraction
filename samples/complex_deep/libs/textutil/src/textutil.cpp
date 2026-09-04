#include "textutil/textutil.hpp"
#include "core/core.hpp"
#include <boost/algorithm/string.hpp>

namespace textutil {
std::vector<std::string> fields(const std::string& line, char delim) {
  std::vector<std::string> out;
  boost::split(out, line, boost::is_any_of(std::string(1, delim)));
  for (auto& f : out) boost::trim(f);
  return out;
}
std::string join_upper(const std::vector<std::string>& parts) {
  std::vector<std::string> up = parts;
  for (auto& p : up) boost::to_upper(p);
  return boost::join(up, "|") + "#" + std::to_string(core::width_of((int)up.size()));
}
}  // namespace textutil
