#include "mathx/mathx.hpp"
#include "internal.hpp"
#include "base/base.hpp"
namespace mathx { int outer() { return base::seed() + secret(); } }
