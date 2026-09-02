#include "input/input.hpp"

#include <fmt/color.h>
#include <fmt/core.h>

#include <istream>
#include <ostream>

namespace input {

std::optional<std::string> read_line(std::istream& in) {
  std::string line;
  if (!std::getline(in, line)) return std::nullopt;
  return line;
}

std::optional<int> read_int(std::istream& in, std::ostream& out,
                            const std::string& prompt, int lo, int hi) {
  while (true) {
    out << fmt::format(fmt::fg(fmt::color::cyan), "{} [{}-{}]: ", prompt, lo, hi);
    auto line = read_line(in);
    if (!line) return std::nullopt;

    try {
      std::size_t pos = 0;
      int value = std::stoi(*line, &pos);
      if (value >= lo && value <= hi) return value;
    } catch (...) {
      // Not a number: fall through to the retry message.
    }
    out << fmt::format(fmt::fg(fmt::color::red), "  invalid, try again\n");
  }
}

}  // namespace input
