#include "geom/geom.hpp"
#include <boost/geometry.hpp>
#include <boost/geometry/geometries/point_xy.hpp>
#include <boost/geometry/geometries/polygon.hpp>

namespace geom {
double hull_area() {
  using point = boost::geometry::model::d2::point_xy<double>;
  boost::geometry::model::polygon<point> poly;
  boost::geometry::read_wkt("POLYGON((0 0,0 4,4 4,4 0,0 0))", poly);
  boost::geometry::model::polygon<point> hull;
  boost::geometry::convex_hull(poly, hull);
  return boost::geometry::area(hull);
}
}  // namespace geom
