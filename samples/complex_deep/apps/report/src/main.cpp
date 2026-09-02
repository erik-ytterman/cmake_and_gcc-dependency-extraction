#include "data/data.hpp"
#include "text/text.hpp"
#include <cstdio>
int main() { std::puts(text::banner(data::to_json(42)).c_str()); return 0; }
