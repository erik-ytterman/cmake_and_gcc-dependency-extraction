#pragma once

#include <iosfwd>
#include <optional>
#include <string>

namespace input {

// Read one line from `in`. Returns nullopt on EOF/error.
std::optional<std::string> read_line(std::istream& in);

// Prompt on `out` (colored) and read an integer in the inclusive range
// [lo, hi] from `in`. Retries on invalid or out-of-range input. Returns
// nullopt on EOF. Streams are injectable so the logic is unit-testable.
std::optional<int> read_int(std::istream& in, std::ostream& out,
                            const std::string& prompt, int lo, int hi);

}  // namespace input
