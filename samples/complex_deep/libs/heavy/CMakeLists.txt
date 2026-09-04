# Libraries belonging to the rest of the monorepo. Nothing the extracted
# application links reaches any of them -- and each pulls in one of Boost's
# expensive header trees, which is what makes leaving them behind measurable.
add_subdirectory(geom)
add_subdirectory(netsvc)
add_subdirectory(parsing)
