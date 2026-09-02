#include "net/net.hpp"
int main() { return net::handshake().empty() ? 1 : 0; }
