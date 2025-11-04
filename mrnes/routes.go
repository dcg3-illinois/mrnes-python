package mrnes

// routes.go provides functions to create and access shortest path routes through a MrNesbit network

import (
	"fmt"
	"math"
	"strconv"
	"strings"

	"gonum.org/v1/gonum/graph"
	"gonum.org/v1/gonum/graph/path"
	"gonum.org/v1/gonum/graph/simple"
)

// The general approach we use is to convert a MrNesBits representation of the network
// into the data structures used by a graph package that has built-in path discovery algorithms.
// Weighting each edge by 1, a shortest path minimizes the number of hops, which is sort of what
// local routing like OSPF does.
//   The network representation from MrNesbits represents devices (Endpt, Switch, Router), with
// a link from devA to devB being represented if there is a direct wired or wireless connection
// between them.  After a path is computed from device to device to device ... we discover the identity
// of the interface through which the path accesses the device, and report that as part of the path.
//
//   The Dijsktra algorithm we call computes a tree of shortest paths from a named node.
// so then if we want the shortest path from src to dst, we either compute such a tree rooted in
// src, or look up from a cached version of an already computed tree the sequence of nodes
// between src and dst, inclusive. Failing that we look for a known path from dst to src, which
// will by symmetry be the reversed path of what we want.

// ShowPath returns a string that lists the names of all the network devices on a
// path. The input arguments are the source and destination device ids, a dictionary holding string
// names as a function of device id, representation the path, and a flag indicating whether the path
// is from src to dst, or vice-versa
func ShowPath(src int, dest int, idToName map[int]string, thru map[int]int,
	forwardDirection bool) string {

	// sequence will hold the names of the devices on the path, in the reverse order they are visited
	sequence := make([]string, 0)
	here := dest

	// the 'thru' map identifies the point-to-point connections on the path, in reverse order
	// (a result of the algorithm used to discover the path).
	// Loop until the path discovered backwards takes us to the source
	for here != src {
		sequence = append(sequence, idToName[here])
		here = thru[here]
	}
	sequence = append(sequence, idToName[src])

	// put the sequence of device names into the pathString list. If input
	// argument forwardDirection is true then the order we want in sequence is in reverse
	// order in sequence.
	pathString := make([]string, 0)
	if forwardDirection {
		for idx := len(sequence) - 1; idx > -1; idx-- {
			pathString = append(pathString, sequence[idx])
		}
	} else {
		pathString = append(pathString, sequence...)
	}

	return strings.Join(pathString, ",")
}

// the gNodes data structure implements a graph representation of the MrNesbits network
// in a form that lets us use the graph module.
// gNodes[i] refers to the MrNesbits network device with id i
var gNodes map[int]simple.Node

// cachedSP saves the resultof computing shortest-path trees.
// The key is the device id of the path source, the value is the graph/path representation of the tree
var cachedSP map[int]path.Shortest

// buildconnGraph returns a graph.Graph data structure built from
// a MrNesbits representation of a node id and a list of node ids of
// network devices it connects to.
func buildconnGraph(edges map[int][]int) graph.Graph {
	gNodes = make(map[int]simple.Node)
	cachedSP = make(map[int]path.Shortest)
	connGraphBuilt = false
	// connGraph := simple.NewWeightedUndirectedGraph(0, math.Inf(1))
	connGraph := simple.NewWeightedDirectedGraph(0, math.Inf(1))
	for nodeID := range edges {
		_, present := gNodes[nodeID]
		if present {
			continue
		}
		gNodes[nodeID] = simple.Node(nodeID)
	}

	// transform the expression of edges input list to edges in the graph module representation

	// MrNesbits device id and list of ids of edges it connects to
	for nodeID, edgeList := range edges {
		// for every neighbor in that list
		for _, nbrID := range edgeList {
			// represent the edge (with weight 1) in the form that the graph module represents it
			if directedIDToDev[nodeID] == directedIDToDev[nbrID] {
				weightedEdge := simple.WeightedEdge{F: gNodes[nodeID], T: gNodes[nbrID], W: 0.0}
				connGraph.SetWeightedEdge(weightedEdge)
			} else {
				weightedEdge := simple.WeightedEdge{F: gNodes[nodeID], T: gNodes[nbrID], W: 1.0}
				connGraph.SetWeightedEdge(weightedEdge)
			}
		}
	}
	// set the flag to show we've done it and so don't need to do it again
	connGraphBuilt = true
	return connGraph
}

// getSPTree returns the shortest path tree rooted in input argument 'from' from
// tree 'connGraph'.  If the tree is found in the cache it is returned, if not it is computed, saved, and returned.
func getSPTree(from int, connGraph graph.Graph) path.Shortest {
	// look for existence of tree already
	spTree, present := cachedSP[from]
	if present {
		// yes, we're done
		return spTree
	}

	// let graph/path.DijkstraFrom compute the tree. The first argument
	// is the root of the tree, the second is the graph
	spTree = path.DijkstraFrom(gNodes[from], connGraph)

	// save (using the MrNesbits identity for the node) and return
	cachedSP[from] = spTree

	return spTree
}

// convertNodeSeq extracts the MrNesbits network device ids from a sequence of graph nodes
// (e.g. like a path) and returns that list
func convertNodeSeq(nsQ []graph.Node) []int {
	rtn := []int{}
	var prevNodeID int = -1
	for _, node := range nsQ {
		nodeID, _ := strconv.Atoi(fmt.Sprintf("%d", node))

		// skip when adjacent nodes are pointing to the same device
		if prevNodeID > -1 && directedIDToDev[prevNodeID] == directedIDToDev[nodeID] {
			prevNodeID = nodeID
			continue
		}
		rtn = append(rtn, directedIDToDev[nodeID])
		prevNodeID = nodeID
	}
	return rtn
}

// connGraphBuilt is a flag which is initialized to false but is set true
// once the connection graph is constructed.
var connGraphBuilt bool

// connGraph is the path/graph representation of the mrnes network graph
var connGraph graph.Graph

// routeFrom returns the shortest path (as a sequence of network device identifiers)
// from the named source to the named destination, through the provided graph (in mrnes
// format of device ids)
func routeFrom(srcID int, edges map[int][]int, dstID int) []int {
	// make sure we've built the path/graph respresentation
	if !connGraphBuilt {
		// buildconnGraph creates the graph, and sets the connGraphBuilt flag
		connGraph = buildconnGraph(edges)
	}

	// nodeSeq holds the desired path expressed as a sequence of path/graph nodes
	var nodeSeq []graph.Node

	// route holds the desired path expressed as a sequence of mrnes device ids,
	// ultimately what routeFrom returns
	var route []int

	// if we have already an spTree rooted in srcID we can use it.
	spTree, present := cachedSP[srcID]

	if present {
		// get the path through the tree to the node with label dstID
		// (representing the MrNesbits device with that label)
		nodeSeq, _ = spTree.To(int64(dstID))

		// convert the sequence of graph/path nodes to a sequence of MrNesbits device ids
		route = convertNodeSeq(nodeSeq)
	} else {
		// it may be that we have already a shortest path tree that is routed in the destination.
		// if so, by symmetry the path is the same, just reversed.
		spTree, present = cachedSP[dstID]
		if present {
			// get the path from the graph node with label dstID to the graph node with label srcID
			revNodeSeq, _ := spTree.To(int64(srcID))

			// convert that sequence of graph nodes to a sequence of MrNesbits device ids
			revRoute := convertNodeSeq(revNodeSeq)

			// these are reverse order, so turn them back around
			lenR := len(revRoute)
			for idx := 0; idx < lenR; idx++ {
				route = append(route, revRoute[lenR-idx-1])
			}
		} else {
			// we don't have a tree routed in either srcID or dstID, so make a tree rooted in srcID
			spTree = getSPTree(srcID, connGraph)

			// get the path as a sequence of graph nodes, convert to a sequence of MrNesbits device id values
			nodeSeq, _ = spTree.To(int64(dstID))
			route = convertNodeSeq(nodeSeq)
		}
	}

	return route
}

// rtEndpts holds the IDs of the starting and ending points of a route
type rtEndpts struct {
	srcID, dstID int
}

// commonNetID checks that intrfcA and intrfcB point at the same network and returns its name
func commonNetID(intrfcA, intrfcB *intrfcStruct) int {
	if intrfcA.Faces == nil || intrfcB.Faces == nil {
		panic("interface on route fails to face any network")
	}

	if intrfcA.Faces.Name != intrfcB.Faces.Name {
		panic("interfaces on route do not face the same network")
	}
	return intrfcA.Faces.Number
}

// intrfcsBetween computes identify of the interfaces between routeSteps rsA and rsB
func intrfcsBetween(rsA, rsB int) (int, int) {
	return getRouteStepIntrfcs(rsA, rsB)
}

// pcktRtCache is a cache holding routes that have been discovered, so that
// they do not need to be rediscovered
var pcktRtCache map[rtEndpts]*[]intrfcsToDev = make(map[rtEndpts]*[]intrfcsToDev)

// findRoute analyzes the topology to discover a least-cost sequence
// of interfaces through which to pass in order to pass from the source device
// whose ID is given to the destination device
func findRoute(srcID, dstID int) *[]intrfcsToDev {
	endpoints := rtEndpts{srcID: srcID, dstID: dstID}

	// if we found and cached it already, return the cached route
	rt, found := pcktRtCache[endpoints]
	if found {
		return rt
	}

	// routeFrom will return a sequence of device IDs, which
	// need to expanded to carry all of the information about a routing
	// step that is expected when traversing a route.  Use the directed graph
	// representation to avoid having endpts be transit devices in routes
	route := routeFrom(devIDToDirected[srcID].i, directedTopoGraph, devIDToDirected[dstID].j)

	// struct intrfcsToDev describes a step in the route
	routePlan := make([]intrfcsToDev, 0)

	// touch each device named in the route discovered from the graph analysis
	for idx := 1; idx < len(route); idx++ {

		// what's the ID of the device at this step of the route?
		devID := route[idx]

		// what are the identities of the interface out of which the route passes
		// from the previous device and the interface into which the route passes to reach devID
		srcIntrfcID, dstIntrfcID := intrfcsBetween(route[idx-1], devID)

		dstIntrfc := IntrfcByID[dstIntrfcID]

		// if 'cable' is nil we're pointing through a network and
		// use its id
		// if dstIntrfc.Cable == nil {
		//		networkID = dstIntrfc.Faces.Number
		// }
		networkID := dstIntrfc.Faces.Number

		istp := intrfcsToDev{srcIntrfcID: srcIntrfcID, dstIntrfcID: dstIntrfcID, netID: networkID, devID: devID}
		routePlan = append(routePlan, istp)
	}
	pcktRtCache[endpoints] = &routePlan

	return &routePlan
}
