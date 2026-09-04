#include "netsvc/netsvc.hpp"
#include <boost/asio.hpp>

namespace netsvc {
int tick_count() {
  boost::asio::io_context io;
  int n = 0;
  boost::asio::steady_timer t(io, std::chrono::milliseconds(1));
  t.async_wait([&n](const boost::system::error_code&) { ++n; });
  io.run();
  return n;
}
}  // namespace netsvc
