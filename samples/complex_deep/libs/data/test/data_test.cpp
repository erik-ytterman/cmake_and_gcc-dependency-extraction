#include "data/data.hpp"
int main() { return data::to_json(1).find("value") == std::string::npos ? 1 : 0; }
