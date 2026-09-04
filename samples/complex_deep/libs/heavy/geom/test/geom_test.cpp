#include "geom/geom.hpp"
#include <cassert>
#include <cmath>
#include <cstdio>
int main() { assert(std::fabs(geom::hull_area() - 16.0) < 1e-9); std::puts("geom_test passed"); }
