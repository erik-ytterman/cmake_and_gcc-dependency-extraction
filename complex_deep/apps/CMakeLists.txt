# Each app touches a different subset of the library tree and the dependencies.
#
#   app       first-party            third-party
#   --------  --------------------   ------------------------------
#   calc      mathx base             (none)
#   render    text base              fmt          (via text, PUBLIC)
#   daemon    net codec base         Threads      (via net, PUBLIC)
#   report    data text base         fmt json
#   omni      everything             fmt json Threads
add_subdirectory(calc)
add_subdirectory(render)
add_subdirectory(daemon)
add_subdirectory(report)
add_subdirectory(omni)
