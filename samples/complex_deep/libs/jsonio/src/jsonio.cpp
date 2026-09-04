#include "jsonio/jsonio.hpp"
#include "core/core.hpp"
#include <nlohmann/json.hpp>

namespace jsonio {
std::string encode(const std::string& label, double value) {
  nlohmann::json j;
  j["label"] = label;
  j["value"] = value;
  j["width"] = core::width_of(static_cast<int>(value));
  return j.dump();
}
double value_of(const std::string& json_text) {
  return nlohmann::json::parse(json_text).at("value").get<double>();
}
}  // namespace jsonio
