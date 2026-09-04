#include <cstdio>
#include <string>
#include <vector>

#include <complex_deep/version.hpp>

#include "core/codec/codec.hpp"
#include "core/core.hpp"
#include "jsonio/jsonio.hpp"
#include "textutil/textutil.hpp"

// The one application in this sample. It reaches core, core/codec, jsonio and
// textutil -- and through them Boost (string algorithms) and nlohmann_json.
// It never reaches geom, netsvc or parsing, which is the point: those three
// pull in Boost's expensive header trees and stay behind on extraction.
int main(int argc, char** argv) {
  const std::string line = (argc > 1) ? argv[1] : " temp , 21.5 , celsius ";

  std::printf("report %s\n", complex_deep::kVersion);

  const std::vector<std::string> parts = textutil::fields(line, ',');
  std::printf("  fields : %s\n", textutil::join_upper(parts).c_str());

  const double value = parts.size() > 1 ? std::stod(parts[1]) : 0.0;
  const std::string doc = jsonio::encode(parts.empty() ? "?" : parts[0], value);
  std::printf("  json   : %s\n", doc.c_str());
  std::printf("  read   : %.2f\n", jsonio::value_of(doc));
  std::printf("  tag    : %s\n", core::tag(static_cast<int>(value)).c_str());
  std::printf("  hex    : %s\n", core::codec::hex(static_cast<int>(value)).c_str());
  return 0;
}
