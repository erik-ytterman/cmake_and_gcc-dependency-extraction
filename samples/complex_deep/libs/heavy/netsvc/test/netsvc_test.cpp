#include "netsvc/netsvc.hpp"
#include <cassert>
#include <cstdio>
int main() { assert(netsvc::tick_count() == 1); std::puts("netsvc_test passed"); }
