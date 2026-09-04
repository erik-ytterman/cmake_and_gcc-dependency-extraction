#include "parsing/parsing.hpp"
#include <boost/spirit/home/x3.hpp>

namespace parsing {
std::vector<double> numbers(const std::string& in) {
  namespace x3 = boost::spirit::x3;
  std::vector<double> out;
  auto first = in.begin(), last = in.end();
  x3::phrase_parse(first, last, x3::double_ % ',', x3::space, out);
  return out;
}
}  // namespace parsing
