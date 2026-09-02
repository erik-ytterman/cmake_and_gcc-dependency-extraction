#include "data/data.hpp"
#include "base/base.hpp"
#include <nlohmann/json.hpp>
namespace data {
std::string to_json(int value) {
  nlohmann::json j;
  j["seed"] = base::seed();
  j["value"] = value;
  return j.dump();
}
}
