#include "mathx/mathx.hpp"
#include "text/text.hpp"
#include "net/net.hpp"
#include "data/data.hpp"
#include "complex_deep/version.hpp"
#include <cstdio>
int main() {
  std::printf("omni %s: %d %s %s %s\n", complex_deep::kVersion,
              mathx::outer() + mathx::inner(),
              text::banner("x").c_str(), net::handshake().c_str(),
              data::to_json(1).c_str());
  return 0;
}
