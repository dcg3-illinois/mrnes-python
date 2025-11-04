"""
Python translation of mrnes/routes.go
Provides functions to create and access shortest path routes through a MrNesbit network.

This module preserves comments from the original Go source and maps Gonum graph usage
to NetworkX where possible.
"""

from typing import Dict, List, Tuple
import math

# We will use networkx as the graph library equivalent to Gonum's graph/path
import networkx as nx

# Placeholder globals expected to be provided by the project
# directedIDToDev: dict mapping directed node id to device id
# directedTopoGraph: a NetworkX DiGraph representing directed topology
# devIDToDirected: map from device id to a struct/tuple with .i and .j directed ids
# IntrfcByID: mapping from interface id to interface structures
# getRouteStepIntrfcs: function to get interface ids between two device ids
# These must be defined elsewhere in the project

# the gNodes data structure implements a graph representation of the MrNesbits network
# in a form that lets us use the graph module.
# gNodes[i] refers to the MrNesbits network device with id i
g_nodes: Dict[int, int] = {}

# cachedSP saves the result of computing shortest-path trees.
# The key is the device id of the path source, the value is the graph/path representation of the tree
cached_sp: Dict[int, object] = {}

# connGraphBuilt is a flag which is initialized to false but is set true
# once the connection graph is constructed.
conn_graph_built: bool = False

# connGraph is the path/graph representation of the mrnes network graph
conn_graph = None


def show_path(src: int, dest: int, id_to_name: Dict[int, str], thru: Dict[int, int], forward_direction: bool) -> str:
    """
    ShowPath returns a string that lists the names of all the network devices on a
    path. The input arguments are the source and destination device ids, a dictionary holding string
    names as a function of device id, representation the path, and a flag indicating whether the path
    is from src to dst, or vice-versa
    """
    # sequence will hold the names of the devices on the path, in the reverse order they are visited
    sequence: List[str] = []
    here = dest

    # the 'thru' map identifies the point-to-point connections on the path, in reverse order
    # (a result of the algorithm used to discover the path).
    # Loop until the path discovered backwards takes us to the source
    while here != src:
        sequence.append(id_to_name[here])
        here = thru[here]
    sequence.append(id_to_name[src])

    # put the sequence of device names into the pathString list. If input
    # argument forwardDirection is true then the order we want in sequence is in reverse
    # order in sequence.
    path_string: List[str] = []
    if forward_direction:
        path_string = sequence[::-1]
    else:
        path_string.extend(sequence)

    return ",".join(path_string)


def build_conn_graph(edges: Dict[int, List[int]]):
    """
    buildconnGraph returns a graph.Graph data structure built from
    a MrNesbits representation of a node id and a list of node ids of
    network devices it connects to.
    """
    global g_nodes, cached_sp, conn_graph_built
    g_nodes = {}
    cached_sp = {}
    conn_graph_built = False

    # Use networkx directed graph with weight on edges
    conn_graph = nx.DiGraph()

    # create nodes
    for node_id in edges.keys():
        g_nodes[node_id] = node_id

    # transform edges
    for node_id, edge_list in edges.items():
        for nbr_id in edge_list:
            # represent the edge (with weight 1) in the form that the graph module represents it
            # If directedIDToDev[nodeID] == directedIDToDev[nbrID], weight 0, else weight 1
            try:
                if directedIDToDev[node_id] == directedIDToDev[nbr_id]:
                    w = 0.0
                else:
                    w = 1.0
            except Exception:
                w = 1.0
            # nodes are added automatically when adding edges
            conn_graph.add_edge(node_id, nbr_id, weight=w)

    conn_graph_built = True
    return conn_graph


def get_sp_tree(frm: int, conn_graph):
    """
    getSPTree returns the shortest path tree rooted in input argument 'frm' from
    tree 'connGraph'. If the tree is found in the cache it is returned, if not it is computed, saved, and returned.
    """
    global cached_sp
    if frm in cached_sp:
        return cached_sp[frm]

    # Use Dijkstra shortest paths (single-source)
    sp_tree = nx.single_source_dijkstra_path(conn_graph, frm, weight="weight")
    
    # save (using the MrNesbits identity for the node) and return
    cached_sp[frm] = sp_tree

    return sp_tree


def convert_node_seq(nsq: List[int]) -> List[int]:
    """
    convertNodeSeq extracts the MrNesbits network device ids from a sequence of graph nodes
    (e.g. like a path) and returns that list
    """
    rtn: List[int] = []
    prev_node_id = -1
    for node in nsq:
        node_id = int(node)
        # skip when adjacent nodes are pointing to the same device
        try:
            if prev_node_id > -1 and directedIDToDev[prev_node_id] == directedIDToDev[node_id]:
                prev_node_id = node_id
                continue

            rtn.append(directedIDToDev[node_id])
            prev_node_id = node_id
        except Exception:
            rtn.append(directedIDToDev[node_id])
            prev_node_id = node_id
    return rtn


def route_from(src_id: int, edges: Dict[int, List[int]], dst_id: int) -> List[int]:
    """
    routeFrom returns the shortest path (as a sequence of network device identifiers)
    from the named source to the named destination, through the provided graph (in mrnes
    format of device ids)
    """
    global conn_graph_built, conn_graph
    if not conn_graph_built:
        conn_graph = build_conn_graph(edges)

    route: List[int] = []

    # If we have already an spTree rooted in srcID we can use it
    if src_id in cached_sp:
        sp_tree = cached_sp[src_id]

        # Get path from src to dst
        node_seq = sp_tree.get(dst_id, [])

        # convert the sequence of graph/path nodes to a sequence of MrNesbits device ids
        route = convert_node_seq(node_seq)
    # it may be that we have already a shortest path tree that is routed in the destination.
    # if so, by symmetry the path is the same, just reversed.
    else:
        if dst_id in cached_sp:
            sp_tree = cached_sp[dst_id]

            # get path from dst to src
            rev_node_seq = sp_tree.get(src_id, [])

            # convert the sequence of graph/path nodes to a sequence of MrNesbits device ids
            rev_route = convert_node_seq(rev_node_seq)

            # reversed order, so get them in the right order
            route = list(reversed(rev_route))
        else:
            # we don't have a tree routed in either srcID or dstID, so make a tree rooted in srcID
            sp_tree = get_sp_tree(src_id, conn_graph)
            
            node_seq = sp_tree.get(dst_id, [])
            route = convert_node_seq(node_seq)
    
    return route


# rtEndpts holds the IDs of the starting and ending points of a route
class RtEndpts:
    def __init__(self, src_id: int, dst_id: int):
        self.src_id = src_id
        self.dst_id = dst_id


def common_net_id(intrfcA, intrfcB) -> int:
    """
    commonNetID checks that intrfcA and intrfcB point at the same network and returns its name
    """
    if getattr(intrfcA, 'Faces', None) is None or getattr(intrfcB, 'Faces', None) is None:
        raise RuntimeError("interface on route fails to face any network")
    if intrfcA.Faces.Name != intrfcB.Faces.Name:
        raise RuntimeError("interfaces on route do not face the same network")
    return intrfcA.Faces.Number


def intrfcs_between(rsA: int, rsB: int) -> Tuple[int, int]:
    """intrfcsBetween computes identify of the interfaces between routeSteps rsA and rsB"""
    return getRouteStepIntrfcs(rsA, rsB)


# pcktRtCache is a cache holding routes that have been discovered, so that
# they do not need to be rediscovered
pckt_rt_cache: Dict[RtEndpts, List[object]] = {} # TODO: clarify that object is intrfcsToDev

def find_route(src_id: int, dst_id: int) -> List[object]:
    """
    findRoute analyzes the topology to discover a least-cost sequence
    of interfaces through which to pass in order to pass from the source device
    whose ID is given to the destination device
    """
    endpoints = RtEndpts(src_id, dst_id)

    if endpoints in pckt_rt_cache:
        return pckt_rt_cache[endpoints]

    # routeFrom will return a sequence of device IDs, which
    # need to expanded to carry all of the information about a routing
    # step that is expected when traversing a route. Use the directed graph
    # representation to avoid having endpts be transit devices in routes
    route = route_from(devIDToDirected[src_id].i, directedTopoGraph, devIDToDirected[dst_id].j) # TODO: define directedTopoGraph and devIDToDirected

    route_plan: List[object] = [] # TODO: clarify that object is intrfcsToDev

    # touch each device named in the route discovered from the graph analysis
    for idx in range(1, len(route)):
        
        # what's the ID of the device at this step of the route?
        dev_id = route[idx]

        # what are the identities of the interface out of which the route passes
        # from the previous device and the interface into which the route passes to reach devID
        src_intrfc_id, dst_intrfc_id = intrfcs_between(route[idx - 1], dev_id)

        dst_intrfc = IntrfcByID[dst_intrfc_id] # TODO: define IntrfcByID
        
        # if 'cable' is nil we're pointing through a network and
        # use its id
        # if dstIntrfc.Cable == nil {
        #        networkID = dstIntrfc.Faces.Number
        # }
        network_id = dst_intrfc.Faces.Number

        # TODO: define interfcsToDev
        istp = interfcsToDev(src_intrfc_id, dst_intrfc_id, network_id, dev_id)
        
        route_plan.append(istp)

    pckt_rt_cache[endpoints] = route_plan

    return route_plan
