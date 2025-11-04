package mrnes

// nets.go contains code and data structures supporting the
// simulation of traffic through the communication network.
// mrnes supports passage of discrete packets.  These pass
// through the interfaces of devices (routers and switch) on the
// shortest path route between endpoints, accumulating delay through the interfaces
// and across the networks, as a function of overall traffic load (most particularly
// including the background flows)
import (
	"fmt"
	"math"
	_ "os"
	"sort"
	"strconv"
	"strings"

	"github.com/iti/evt/evtm"
	"github.com/iti/evt/vrtime"
	"github.com/iti/rngstream"
	"golang.org/x/exp/slices"
	"gopkg.in/yaml.v3"
)

// The mrnsbit network simulator is built around two strong assumptions that
// simplify implementation, but which may have to be addressed if mrnsbit
// is to be used when fine-grained networking details are thought to be important.
//
// The first assumption is that routing is static, that the complete route for
// a message from specified source to specified destination can be computed at
// the time the message enters the network.   It happens that this implementation
// uses minimum hop count as the metric for choosing routes, but this is not so rigourously
// embedded in the design as is static routes.
//
// The second assumption is related to the reality that messages do not traverse a link or
// pass through an interface instaneously, they 'connect' across links, through networks, and through devices.
// Flows have bandwidth, and every interface, link, device, and network has its own bandwidth
// limit.  From the point of view of the receiving endpt, the effective bandwidth (assuming the bandwidths
// all along the path don't change) is the minimum bandwidth among all the things on the path.
// In the path before the hop that induces the least bandwidth message may scoot through connections
// at higher bandwidth, but the progress of that connection is ultimately limited  by the smallest bandwidth.
// The simplifying assumption then is that the connection of the message's bits _everywhere_ along the path
// is at that minimum bandwidth.   More detail in tracking and changing bandwidths along sections of the message
// are possible, but at this point it is more complicated and time-consuming to do that than it seems to be worth
// for anticipated use-cases.
//

// intPair, intrfcIDPair, intrfcRate and floatPair are
// structs introduced to add more than basic elements to lists and maps
type intPair struct {
	i, j int
}

type intrfcIDPair struct {
	prevID, nextID int
}

// intrfcQStruct holds information about packets and flows at an interface
type intrfcQStruct struct {
	intrfc    *intrfcStruct // interface this is attached to
	ingress   bool          // true if this queue is for the ingress side of the interface
	streaming bool          // true if there are strm packets at this interface
	lambda    float64       // sum of rates of flows approaching this side of the interface
	msgQueue  []*iQWrapper  // messages enqueued for passage through the interface
	strmQ     *strmSet      // represents the stream of background packets through this side of the interface
}

type iQWrapper struct {
	arrival float64
	nm      *NetworkMsg
}

// createIntrfcQueue is a constructor
func createIntrfcQueue(intrfc *intrfcStruct, ingress bool) *intrfcQStruct {
	iqs := new(intrfcQStruct)
	iqs.intrfc = intrfc
	iqs.ingress = ingress
	iqs.msgQueue = make([]*iQWrapper, 0)
	return iqs
}

func (iqs *intrfcQStruct) qlen() int {
	return len(iqs.msgQueue)
}

func (iqs *intrfcQStruct) firstMsg() *NetworkMsg {
	if len(iqs.msgQueue) > 0 {
		return iqs.msgQueue[0].nm
	}
	return nil
}

// initIntrfcQueueStrm initializes the strmQ structure describing the flows through the interface
func (iqs *intrfcQStruct) initIntrfcQueueStrm(strmRate float64, strmPcktLen int,
	intrfcBndwdth float64, rng *rngstream.RngStream) {
	iqs.strmQ = createStrmSet(iqs, strmRate, intrfcBndwdth, strmPcktLen, rng)
}

func (iqs *intrfcQStruct) popNetworkMsg() *NetworkMsg {
	if len(iqs.msgQueue) == 0 {
		return nil
	}

	var iqw *iQWrapper
	iqw, iqs.msgQueue = iqs.msgQueue[0], iqs.msgQueue[1:]
	return iqw.nm
}

func (iqs *intrfcQStruct) firstNetworkMsg() *NetworkMsg {
	if len(iqs.msgQueue) == 0 {
		return nil
	}

	iqw := iqs.msgQueue[0]
	return iqw.nm
}

func queueStr(msgQueue []*iQWrapper) string {
	rtn := []string{}
	for idx := 0; idx < len(msgQueue); idx += 1 {
		iqw := msgQueue[idx]
		element := fmt.Sprintf("%f %d", iqw.arrival, iqw.nm.MsgID)
		rtn = append(rtn, element)
	}
	result := strings.Join(rtn, ",")
	return result
}

func computeServiceTime(msgLen int, bndwdth float64) float64 {
	msgLenMbits := float64(8*msgLen) / 1e6
	return msgLenMbits / bndwdth
}

// addNetworkMsg adds a network message to the indicated side of the interface
func (iqs *intrfcQStruct) addNetworkMsg(evtMgr *evtm.EventManager, nm *NetworkMsg) {
	time := evtMgr.CurrentSeconds()
	nm.intrfcArr = time

	intrfc := iqs.intrfc

	iqw := new(iQWrapper)
	iqw.arrival = time
	iqw.nm = nm
	iqs.msgQueue = append(iqs.msgQueue, iqw)

	// if we don't have virtual streaming packets and this is an arrival to an empty queue, just enter it into service
	inTransit := (iqs.ingress && iqs.intrfc.State.IngressTransit) || (!iqs.ingress && iqs.intrfc.State.EgressTransit)
	if !iqs.streaming && !inTransit {
		if len(iqs.msgQueue) == 1 {
			enterIntrfcService(evtMgr, iqs, nm.MsgID)
		}
		return
	}

	// the interface streams packets
	if len(iqs.msgQueue) == 1 && !inTransit {
		// strmQ needs to have been running because this message joined an empty queue
		// advance the flow simulation up to time

		nxtMsg := iqs.msgQueue[0].nm
		serviceTime := computeServiceTime(nxtMsg.MsgLen, intrfc.State.Bndwdth)
		advanced, qDelay := iqs.strmQ.queueingDelay(evtMgr.CurrentSeconds(), nxtMsg.intrfcArr,
			serviceTime, nxtMsg.prevIntrfcID)

		msrArrivals = false

		// schedule an entry into service at time
		if advanced {
			evtMgr.Schedule(iqs, nxtMsg.MsgID, enterIntrfcService, vrtime.SecondsToTime(qDelay))
		} else {
			// we go straight into service and so can skip the scheduling step
			enterIntrfcService(evtMgr, iqs, nxtMsg.MsgID)
		}
	}
}

func (iqs *intrfcQStruct) addFlowRate(flowID int, rate float64) {
	iqs.lambda += rate
}

func (iqs *intrfcQStruct) rmFlowRate(flowID int, rate float64) {
	iqs.lambda -= rate
}

// represent the queue state of the intrfcQStruct in a string, for traces
func (iqs *intrfcQStruct) Str() string {
	rtnVec := []string{}
	rtnVec = append(rtnVec, strconv.Itoa(len(iqs.msgQueue)))
	rtnVec = append(rtnVec, strconv.FormatFloat(iqs.lambda, 'g', 12, 64))
	return strings.Join(rtnVec, " % ")
}

// NetworkMsgType give enumeration for message types that may be given to the network
// to carry.  packet is a discrete packet, handled differently from flows.
// srtFlow tags a message that introduces a new flow, endFlow tags one that terminates it,
// and chgFlow tags a message that alters the flow rate on a given flow.
type NetworkMsgType int

const (
	PacketType NetworkMsgType = iota
	FlowType
)

type FlowSrcType int

const (
	FlowSrcConst = iota
	FlowSrcRandom
)

var msrArrivals bool = false

// FlowAction describes the reason for the flow message, that it is starting, ending, or changing the request rate
type FlowAction int

const (
	None FlowAction = iota
	Srt
	Chg
	End
)

// nmtToStr is a translation table for creating strings from more complex
// data types
var nmtToStr map[NetworkMsgType]string = map[NetworkMsgType]string{PacketType: "packet",
	FlowType: "flow"}

// routeStepIntrfcs maps a pair of device IDs to a pair of interface IDs
// that connect them
var routeStepIntrfcs map[intPair]intPair

// getRouteStepIntrfcs looks up the identity of the interfaces involved in connecting
// the named source and the named destination.  These were previously built into a table
func getRouteStepIntrfcs(srcID, dstID int) (int, int) {
	ip := intPair{i: srcID, j: dstID}
	intrfcs, present := routeStepIntrfcs[ip]
	if !present {
		intrfcs, present = routeStepIntrfcs[intPair{j: srcID, i: dstID}]
		if !present {
			panic(fmt.Errorf("no step between %s and %s", TopoDevByID[srcID].DevName(), TopoDevByID[dstID].DevName()))
		}
		return intrfcs.j, intrfcs.i
	}
	return intrfcs.i, intrfcs.j
}

// NetworkPortal implements the pces interface used to pass
// traffic between the application layer and the network sim
type NetworkPortal struct {
	QkNetSim      bool
	ReturnTo      map[int]*rtnRecord // indexed by connectID
	LossRtn       map[int]*rtnRecord // indexed by connectID
	ReportRtnSrc  map[int]*rtnRecord // indexed by connectID
	ReportRtnDst  map[int]*rtnRecord // indexed by connectID
	RequestRate   map[int]float64    // indexed by flowID to get requested arrival rate
	AcceptedRate  map[int]float64    // indexed by flowID to get accepted arrival rate
	Mode          map[int]string     // indexed by flowID to record mode of flow
	Elastic       map[int]bool       // indexed by flowID to record whether flow is elastic
	Pckt          map[int]bool       // indexed by flowID to record whether flow is packet
	Connections   map[int]int        // indexed by connectID to get flowID
	InvConnection map[int]int        // indexed by flowID to get connectID
	LatencyConsts map[int]float64    // indexex by flowID to get latency constants on flow's route
}

// ActivePortal remembers the most recent NetworkPortal created
// (there should be only one call to CreateNetworkPortal...)
var ActivePortal *NetworkPortal

type ActiveRec struct {
	Number int
	Rate   float64
}

// CreateNetworkPortal is a constructor, passed a flag indicating which
// of two network simulation modes to use, passes a flag indicating whether
// packets should be passed whole, and writes the NetworkPortal pointer into a global variable
func CreateNetworkPortal() *NetworkPortal {
	if ActivePortal != nil {
		return ActivePortal
	}
	np := new(NetworkPortal)

	// set default settings
	np.QkNetSim = false
	np.ReturnTo = make(map[int]*rtnRecord)
	np.LossRtn = make(map[int]*rtnRecord)
	np.ReportRtnSrc = make(map[int]*rtnRecord)
	np.ReportRtnDst = make(map[int]*rtnRecord)
	np.RequestRate = make(map[int]float64)
	np.AcceptedRate = make(map[int]float64)
	np.Elastic = make(map[int]bool)
	np.Pckt = make(map[int]bool)
	np.Mode = make(map[int]string)
	np.Connections = make(map[int]int)
	np.InvConnection = make(map[int]int)
	np.LatencyConsts = make(map[int]float64)

	// save the mrnes memory space version
	ActivePortal = np
	connections = 0
	return np
}

// SetQkNetSim saves the argument as indicating whether latencies
// should be computed as 'Placed', meaning constant, given the state of the network at the time of
// computation
func (np *NetworkPortal) SetQkNetSim(quick bool) {
	np.QkNetSim = quick
}

// ClearRmFlow removes entries from maps indexed
// by flowID and associated connectID, to help clean up space
func (np *NetworkPortal) ClearRmFlow(flowID int) {
	connectID := np.InvConnection[flowID]
	delete(np.ReturnTo, connectID)
	delete(np.LossRtn, connectID)
	delete(np.ReportRtnSrc, connectID)
	delete(np.ReportRtnDst, connectID)
	delete(np.Connections, connectID)

	delete(np.RequestRate, flowID)
	delete(np.AcceptedRate, flowID)
	delete(np.LatencyConsts, flowID)
}

// EndptDevModel helps NetworkPortal implement the pces NetworkPortal interface,
// returning the CPU model associated with a named endpt.  Present because the
// application layer does not otherwise have visibility into the network topology
func (np *NetworkPortal) EndptDevModel(devName string, accelName string) string {
	endpt, present := EndptDevByName[devName]
	if !present {
		return ""
	}
	if len(accelName) == 0 {
		return endpt.EndptModel
	}
	accelModel, present := endpt.EndptAccelModel[accelName]
	if !present {
		return ""
	}
	return accelModel
}

// Depart is called to return an application message being carried through
// the network back to the application layer
func (np *NetworkPortal) Depart(evtMgr *evtm.EventManager, devName string, nm NetworkMsg) {
	connectID := nm.ConnectID

	// may not require knowledge that delivery made it
	rtnRec, present := np.ReturnTo[connectID]
	if !present || rtnRec == nil || rtnRec.rtnCxt == nil {
		return
	}

	rtnRec.prArrvl *= nm.PrArrvl
	rtnRec.pckts -= 1

	// if rtnRec.pckts is not zero there are more packets coming associated
	// with connectID and so we exit
	if rtnRec.pckts > 0 {
		return
	}

	prArrvl := rtnRec.prArrvl

	// so we can return now
	rtnMsg := new(RtnMsgStruct)
	rtnMsg.Latency = evtMgr.CurrentSeconds() - nm.StartTime
	if nm.carriesPckt() {
		rtnMsg.Rate = nm.PcktRate
		rtnMsg.PrLoss = (1.0 - prArrvl)
	} else {
		rtnMsg.Rate = np.AcceptedRate[nm.FlowID]
	}
	rtnMsg.Msg = nm.Msg

	rtnMsg.DevIDs = make([]int, len(*(nm.Route)))
	for idx, rtStep := range *(nm.Route) {
		rtnMsg.DevIDs[idx] = rtStep.devID
	}

	rtnCxt := rtnRec.rtnCxt
	rtnFunc := rtnRec.rtnFunc

	endpt := EndptDevByName[devName]

	// schedule the re-integration into the application simulator, delaying by
	// the interrupt handler cost (if any)
	evtMgr.Schedule(rtnCxt, rtnMsg, rtnFunc, vrtime.SecondsToTime(endpt.EndptState.InterruptDelay))

	delete(np.ReturnTo, connectID)
	delete(np.LossRtn, connectID)
	delete(np.ReportRtnSrc, connectID)
	delete(np.ReportRtnDst, connectID)
}

// requestedLoadFracVec computes the relative requested rate for a flow
// among a list of flows.   Used to rescale accepted rates
func (np *NetworkPortal) requestedLoadFracVec(vec []int) []float64 {
	rtn := make([]float64, len(vec))
	var agg float64

	// gather up the rates in an array and compute the normalizing sum
	for idx, flowID := range vec {
		rate := np.RequestRate[flowID]
		agg += rate
		rtn[idx] = rate
	}

	// normalize
	for idx := range vec {
		rtn[idx] /= agg
	}
	return rtn
}

// RtnMsgStruct formats the report passed from the network to the
// application calling it
type RtnMsgStruct struct {
	Latency float64 // span of time (secs) from srcDev to dstDev
	Rate    float64 // for a flow, its accept rate.  For a packet, the minimum non-flow bandwidth at a
	// network or interface it encountered
	PrLoss float64 // estimated probability of having been dropped somewhere in transit
	DevIDs []int   // list of ids of devices visited on transition of network
	Msg    any     // msg introduced at EnterNetwork
}

// Arrive is called at the point an application message is received by the network
// and a new connectID is created (and returned) to track it.  It saves information needed to re-integrate
// the application message into the application layer when the message arrives at its destination
func (np *NetworkPortal) Arrive(rtns RtnDescs, frames int) int {

	// record how to transition from network to upper layer, through event handler at upper layer
	rtnRec := &rtnRecord{rtnCxt: rtns.Rtn.Cxt, rtnFunc: rtns.Rtn.EvtHdlr, prArrvl: 1.0, pckts: frames}

	connectID := nxtConnectID()
	np.ReturnTo[connectID] = rtnRec

	// if requested, record how to notify source end of connection at upper layer through connection
	if rtns.Src != nil {
		rtnRec = new(rtnRecord)
		*rtnRec = rtnRecord{rtnCxt: rtns.Src.Cxt, rtnFunc: rtns.Src.EvtHdlr, prArrvl: 1.0, pckts: 1}
		np.ReportRtnSrc[connectID] = rtnRec
	}

	// if requested, record how to notify destination end of connection at upper layer through event handler
	if rtns.Dst != nil {
		rtnRec = new(rtnRecord)
		*rtnRec = rtnRecord{rtnCxt: rtns.Dst.Cxt, rtnFunc: rtns.Dst.EvtHdlr, prArrvl: 1.0, pckts: 1}
		np.ReportRtnDst[connectID] = rtnRec
	}

	// if requested, record how to notify occurance of loss at upper layer, through event handler
	if rtns.Loss != nil {
		rtnRec = new(rtnRecord)
		*rtnRec = rtnRecord{rtnCxt: rtns.Loss.Cxt, rtnFunc: rtns.Loss.EvtHdlr, prArrvl: 1.0, pckts: 1}
		np.LossRtn[connectID] = rtnRec
	}
	return connectID
}

// DevCode is the base type for an enumerated type of network devices
type DevCode int

const (
	EndptCode DevCode = iota
	SwitchCode
	RouterCode
	UnknownCode
)

// DevCodeFromStr returns the devCode corresponding to an string name for it
func DevCodeFromStr(code string) DevCode {
	switch code {
	case "Endpt", "Endpoint", "endpt":
		return EndptCode
	case "Switch", "switch":
		return SwitchCode
	case "Router", "router", "rtr":
		return RouterCode
	default:
		return UnknownCode
	}
}

// DevCodeToStr returns a string corresponding to an input devCode for it
func DevCodeToStr(code DevCode) string {
	switch code {
	case EndptCode:
		return "Endpt"
	case SwitchCode:
		return "Switch"
	case RouterCode:
		return "Router"
	case UnknownCode:
		return "Unknown"
	}

	return "Unknown"
}

// NetworkScale is the base type for an enumerated type of network type descriptions
type NetworkScale int

const (
	LAN NetworkScale = iota
	WAN
	T3
	T2
	T1
	GeneralNet
)

// NetScaleFromStr returns the networkScale corresponding to an string name for it
func NetScaleFromStr(netScale string) NetworkScale {
	switch netScale {
	case "LAN":
		return LAN
	case "WAN":
		return WAN
	case "T3":
		return T3
	case "T2":
		return T2
	case "T1":
		return T1
	default:
		return GeneralNet
	}
}

// NetScaleToStr returns a string name that corresponds to an input networkScale
func NetScaleToStr(ntype NetworkScale) string {
	switch ntype {
	case LAN:
		return "LAN"
	case WAN:
		return "WAN"
	case T3:
		return "T3"
	case T2:
		return "T2"
	case T1:
		return "T1"
	case GeneralNet:
		return "GeneralNet"
	default:
		return "GeneralNet"
	}
}

// NetworkMedia is the base type for an enumerated type of comm network media
type NetworkMedia int

const (
	Wired NetworkMedia = iota
	Wireless
	UnknownMedia
)

// NetMediaFromStr returns the networkMedia type corresponding to the input string name
func NetMediaFromStr(media string) NetworkMedia {
	switch media {
	case "Wired", "wired":
		return Wired
	case "wireless", "Wireless":
		return Wireless
	default:
		return UnknownMedia
	}
}

// every new network connection is given a unique connectID upon arrival
var connections int

func nxtConnectID() int {
	connections += 1
	return connections
}

type DFS map[int]intrfcIDPair

// TopoDev interface specifies the functionality different device types provide
type TopoDev interface {
	DevName() string              // every device has a unique name
	DevID() int                   // every device has a unique integer id
	DevType() DevCode             // every device is one of the devCode types
	DevModel() string             // model (or CPU) of device
	DevIntrfcs() []*intrfcStruct  // we can get from devices a list of the interfaces they endpt, if any
	DevDelay(*NetworkMsg) float64 // every device can be be queried for the delay it introduces for an operation
	DevState() any                // every device as a structure of state that can be accessed
	DevRng() *rngstream.RngStream // every device has its own RNG stream
	DevAddActive(*NetworkMsg)     // add the connectID argument to the device's list of active connections
	DevRmActive(int)              // remove the connectID argument to the device's list of active connections
	DevForward() DFS              // index by FlowID, yields map of ingress intrfc ID to egress intrfc ID
	LogNetEvent(vrtime.Time, *NetworkMsg, string)
}

// paramObj interface is satisfied by every network object that
// can be configured at run-time with performance parameters. These
// are intrfcStruct, networkStruct, switchDev, endptDev, routerDev
type paramObj interface {
	matchParam(string, string) bool
	setParam(string, valueStruct)
	paramObjName() string
	LogNetEvent(vrtime.Time, *NetworkMsg, string)
}

// The intrfcStruct holds information about a network interface embedded in a device
type intrfcStruct struct {
	Name      string          // unique name, probably generated automatically
	Groups    []string        // list of groups this interface may belong to
	Number    int             // unique integer id, probably generated automatically
	DevType   DevCode         // device code of the device holding the interface
	Media     NetworkMedia    // media of the network the interface interacts with
	Device    TopoDev         // pointer to the device holding the interface
	PrmDev    paramObj        // pointer to the device holding the interface as a paramObj
	Cable     *intrfcStruct   // For a wired interface, points to the "other" interface in the connection
	Carry     []*intrfcStruct // points to the "other" interface in a connection
	FlowModel string          // code for method for introducing delay due to flows
	Wireless  []*intrfcStruct // For a wired interface, points to the "other" interface in the connection
	Faces     *networkStruct  // pointer to the network the interface interacts with
	State     *intrfcState    // pointer to the interface's block of state information
}

// The  intrfcState holds parameters descriptive of the interface's capabilities
type intrfcState struct {
	Bndwdth    float64 // maximum bandwidth (in Mbytes/sec)
	BufferSize float64 // buffer capacity (in Mbytes)
	Latency    float64 // time the leading bit takes to traverse the wire out of the interface
	Delay      float64 // time the leading bit takes to traverse the interface

	IngressTransit      bool
	IngressTransitMsgID int
	EgressTransit       bool
	EgressTransitMsgID  int

	MTU   int  // maximum packet size (bytes)
	Trace bool // switch for calling add trace
	Drop  bool // whether to permit packet drops

	ToIngress   map[int]float64 // sum of flow rates into ingress side of device
	ThruIngress map[int]float64 // sum of flow rates out of ingress side of device
	ToEgress    map[int]float64 // sum of flow rates into egress side of device
	ThruEgress  map[int]float64 // sum of flow rates out of egress side device

	IngressLambda   float64 // sum of rates of flows approach interface from ingress side.
	EgressLambda    float64
	FlowModel       string
	IngressIntrfcQ  *intrfcQStruct
	EgressIntrfcQ   *intrfcQStruct
	StrmServiceTime float64
}

// createIntrfcState is a constructor, assumes defaults on unspecified attributes
func createIntrfcState(intrfc *intrfcStruct) *intrfcState {
	iss := new(intrfcState)
	iss.Delay = 1e+6 // in seconds!  Set this way so that if not initialized we'll notice
	iss.Latency = 1e+6
	iss.MTU = 1560 // in bytes Set for Ethernet2 MTU, should change if wireless

	iss.ToIngress = make(map[int]float64)
	iss.ThruIngress = make(map[int]float64)
	iss.ToEgress = make(map[int]float64)
	iss.ThruEgress = make(map[int]float64)
	iss.EgressLambda = 0.0
	iss.IngressLambda = 0.0
	iss.FlowModel = "expon"
	iss.IngressIntrfcQ = createIntrfcQueue(intrfc, true)

	rng := intrfc.Device.DevRng()

	// put in default stream rate of zero and bandwidth of 1000, to
	// be modified as updated.
	iss.IngressIntrfcQ.initIntrfcQueueStrm(0.0, 1000, 1000.0, rng)
	iss.EgressIntrfcQ = createIntrfcQueue(intrfc, false)
	iss.EgressIntrfcQ.initIntrfcQueueStrm(0.0, 1000, 1000.0, rng)
	return iss
}

// createIntrfcStruct is a constructor, building an intrfcStruct
// from a desc description of the interface
func createIntrfcStruct(intrfc *IntrfcDesc) *intrfcStruct {
	is := new(intrfcStruct)
	is.Groups = intrfc.Groups

	// name comes from desc description
	is.Name = intrfc.Name

	// unique id is locally generated
	is.Number = nxtID()

	// desc representation codes the device type as a string
	switch intrfc.DevType {
	case "Endpt":
		is.DevType = EndptCode
	case "Router":
		is.DevType = RouterCode
	case "Switch":
		is.DevType = SwitchCode
	}

	// The desc description gives the name of the device endpting the interface.
	// We can use this to look up the locally constructed representation of the device
	// and save a pointer to it
	is.Device = TopoDevByName[intrfc.Device]
	is.PrmDev = paramObjByName[intrfc.Device]

	// desc representation codes the media type as a string
	switch intrfc.MediaType {
	case "wired", "Wired":
		is.Media = Wired
	case "wireless", "Wireless":
		is.Media = Wireless
	default:
		is.Media = UnknownMedia
	}

	is.Wireless = make([]*intrfcStruct, 0)
	is.Carry = make([]*intrfcStruct, 0)

	is.State = createIntrfcState(is)
	return is
}

// non-preemptive priority
// k=1 is highest priority
// W_k : mean waiting time of class-k msgs
// S_k : mean service time of class-k msg
// lambda_k : arrival rate class k
// rho_k : load of class-k, rho_k = lambda_k*S_k
// R : mean residual of server on arrival : (server util)*D/2
//
//  W_k = R/((1-rho_{1}-rho_{2}- ... -rho_{k-1})*(1-rho_1-rho_2- ... -rho_k))
//
//  Mean time in system of class-k msg is T_k = W_k+S_k
//
// for our purposes we will use k=0 for least class, and use the formula
//  W_0 = R/((1-rho_{1}-rho_{2}- ... -rho_{k-1})*(1-rho_1-rho_2- ... -rho_{k-1}-rho_0))

// ShortIntrfc stores information we serialize for storage in a trace
type ShortIntrfc struct {
	DevName     string
	Faces       string
	ToIngress   float64
	ThruIngress float64
	ToEgress    float64
	ThruEgress  float64
	FlowID      int
	NetMsgType  NetworkMsgType
	NetMsgID    int
	Rate        float64
	PrArrvl     float64
	Time        float64
}

// Serialize turns a ShortIntrfc into a string, in yaml format
func (sis *ShortIntrfc) Serialize() string {
	var bytes []byte
	var merr error

	bytes, merr = yaml.Marshal(*sis)

	if merr != nil {
		panic(merr)
	}
	return string(bytes[:])
}

// addTrace gathers information about an interface and message
// passing though it, and prints it out
func (intrfc *intrfcStruct) addTrace(label string, nm *NetworkMsg, t float64) {

	// return if we aren't asking for a trace on this interface
	if !intrfc.State.Trace {
		return
	}

	si := new(ShortIntrfc)
	si.DevName = intrfc.Device.DevName()
	si.Faces = intrfc.Faces.Name
	flwID := nm.FlowID
	si.FlowID = flwID
	si.ToIngress = intrfc.State.ToIngress[flwID]
	si.ThruIngress = intrfc.State.ThruIngress[flwID]
	si.ToEgress = intrfc.State.ToEgress[flwID]
	si.ThruEgress = intrfc.State.ThruEgress[flwID]
	si.NetMsgID = nm.MsgID
	si.NetMsgType = nm.NetMsgType
	si.PrArrvl = nm.PrArrvl
	si.Time = t
	siStr := si.Serialize()
	siStr = strings.Replace(siStr, "\n", " ", -1)
	siStr += "\n"
	fmt.Println(label, siStr)
}

// matchParam is used to determine whether a run-time parameter description
// should be applied to the interface. Its definition here helps intrfcStruct satisfy
// paramObj interface.  To apply or not to apply depends in part on whether the
// attribute given matchParam as input matches what the interface has. The
// interface attributes that can be tested are the device type of device that endpts it, and the
// media type of the network it interacts with
func (intrfc *intrfcStruct) matchParam(attrbName, attrbValue string) bool {
	switch attrbName {
	case "name":
		return intrfc.Name == attrbValue
	case "group":
		return slices.Contains(intrfc.Groups, attrbValue)
	case "media":
		return NetMediaFromStr(attrbValue) == intrfc.Media
	case "devtype":
		return DevCodeToStr(intrfc.Device.DevType()) == attrbValue
	case "devname":
		return intrfc.Device.DevName() == attrbValue
	case "faces":
		return intrfc.Faces.Name == attrbValue
	}

	// an error really, as we should match only the names given in the switch statement above
	return false
}

// setParam assigns the parameter named in input with the value given in the input.
// N.B. the valueStruct has fields for integer, float, and string values.  Pick the appropriate one.
// setParam's definition here helps intrfcStruct satisfy the paramObj interface.
func (intrfc *intrfcStruct) setParam(paramType string, value valueStruct) {
	switch paramType {
	// latency, delay, and bandwidth are floats
	case "latency":
		// parameters units of latency are already in seconds
		intrfc.State.Latency = value.floatValue
	case "delay":
		// parameters units of delay are already in seconds
		intrfc.State.Delay = value.floatValue
	case "bandwidth":
		// units of bandwidth are Mbits/sec
		intrfc.State.Bndwdth = value.floatValue
		intrfc.State.IngressIntrfcQ.strmQ.setBndwdth(value.floatValue)
		intrfc.State.EgressIntrfcQ.strmQ.setBndwdth(value.floatValue)
	case "buffer":
		// units of buffer are Mbytes
		intrfc.State.BufferSize = value.floatValue
	case "MTU":
		// number of bytes in maximally sized packet
		intrfc.State.MTU = value.intValue
	case "trace":
		intrfc.State.Trace = (value.intValue == 1)
	case "drop":
		intrfc.State.Drop = value.boolValue
	}
}

// LogNetEvent creates and logs a network event from a message passing
// through this interface
func (intrfc *intrfcStruct) LogNetEvent(time vrtime.Time, nm *NetworkMsg, op string) {
	if !intrfc.State.Trace {
		return
	}
	AddNetTrace(devTraceMgr, time, nm, intrfc.Number, op)
}

// paramObjName helps intrfcStruct satisfy paramObj interface, returns interface name
func (intrfc *intrfcStruct) paramObjName() string {
	return intrfc.Name
}

// linkIntrfcStruct sets the 'connect' and 'faces' values
// of an intrfcStruct based on the names coded in a IntrfcDesc.
func linkIntrfcStruct(intrfcDesc *IntrfcDesc) {
	// look up the intrfcStruct corresponding to the interface named in input intrfc
	is := IntrfcByName[intrfcDesc.Name]

	// in IntrfcDesc the 'Cable' field is a string, holding the name of the target interface
	if len(intrfcDesc.Cable) > 0 {
		_, present := IntrfcByName[intrfcDesc.Cable]
		if !present {
			panic(fmt.Errorf("intrfc cable connection goof"))
		}
		is.Cable = IntrfcByName[intrfcDesc.Cable]
	}

	// in IntrfcDesc the 'Cable' field is a string, holding the name of the target interface
	if len(intrfcDesc.Carry) > 0 {
		for _, cintrfcName := range intrfcDesc.Carry {
			_, present := IntrfcByName[cintrfcName]
			if !present {
				panic(fmt.Errorf("intrfc cable connection goof"))
			}
			is.Carry = append(is.Carry, IntrfcByName[cintrfcName])
		}
	}

	if len(intrfcDesc.Wireless) > 0 {
		for _, IntrfcName := range intrfcDesc.Wireless {
			is.Wireless = append(is.Wireless, IntrfcByName[IntrfcName])
		}
	}

	// in IntrfcDesc the 'Faces' field is a string, holding the name of the network the interface
	// interacts with
	if len(intrfcDesc.Faces) > 0 {
		is.Faces = NetworkByName[intrfcDesc.Faces]
	}
}

// PcktDrop returns a flag indicating whether we're simulating packet drops
func (intrfc *intrfcStruct) PcktDrop() bool {
	return intrfc.State.Drop
}

// AddFlow initializes the To and Thru maps for the interface
func (intrfc *intrfcStruct) AddFlow(flowID int, ingress bool) {
	if ingress {
		intrfc.State.ToIngress[flowID] = 0.0
		intrfc.State.ThruIngress[flowID] = 0.0
	} else {
		intrfc.State.ToEgress[flowID] = 0.0
		intrfc.State.ThruEgress[flowID] = 0.0
	}
}

// IsCongested determines whether the interface is congested,
// meaning that the bandwidth used by elastic flows is greater than or
// equal to the unreserved bandwidth
func (intrfc *intrfcStruct) IsCongested(ingress bool) bool {
	var usedBndwdth float64
	if ingress {
		for _, rate := range intrfc.State.ToIngress {
			usedBndwdth += rate
		}
	} else {
		for _, rate := range intrfc.State.ToEgress {
			usedBndwdth += rate
		}
	}

	// !( bandwidth for elastic flows < available bandwidth for elastic flows ) ==
	// !( usedBndwdth-fixedBndwdth < intrfc.State.Bndwdth-fixedBndwdth )
	// !( usedBndwdth < intrfc.State.Bndwdth )
	//
	bndwdth := intrfc.State.Bndwdth
	return (math.Abs(usedBndwdth-bndwdth) < 1e-3)
}

// ChgFlowRate is called in the midst of changing the flow rates.
// The rate value 'rate' is one that flow has at this point in the
// computation, and the per-flow interface data structures are adjusted
// to reflect that.
// srcIntrfcID identifies the interface from which the flow came to intrfc,
// flowID the flow with the rate change
func (intrfc *intrfcStruct) ChgFlowRate(srcIntrfcID int, flowID int, rate float64, ingress bool) {
	var ifq *intrfcQStruct
	var oldRate float64
	if ingress {
		ifq = intrfc.State.IngressIntrfcQ
		oldRate = intrfc.State.ToIngress[flowID]
		intrfc.State.ToIngress[flowID] = rate
		intrfc.State.ThruIngress[flowID] = rate
		intrfc.State.IngressLambda += (rate - oldRate)
	} else {
		ifq = intrfc.State.EgressIntrfcQ
		oldRate = intrfc.State.ToEgress[flowID]
		intrfc.State.ToEgress[flowID] = rate
		intrfc.State.ThruEgress[flowID] = rate
		intrfc.State.EgressLambda += (rate - oldRate)
	}
	ifq.lambda += (rate - oldRate)
	ifq.strmQ.adjustStrmRate(srcIntrfcID, flowID, ifq.lambda)
}

// RmFlow adjusts data structures to reflect the removal of the identified flow
// formerly having the identified rate
func (intrfc *intrfcStruct) RmFlow(flowID int, rate float64, ingress bool) {
	if ingress {
		delete(intrfc.State.ToIngress, flowID)
		delete(intrfc.State.ThruIngress, flowID)

		// need have the removed flow's prior rate
		// oldRate := cg.ingressLambda
		// cg.ingressLambda = oldRate - rate
	} else {
		delete(intrfc.State.ToEgress, flowID)
		delete(intrfc.State.ThruEgress, flowID)

		// need have the removed flow's prior rate
		// oldRate := cg.egressLambda
		// cg.egressLambda = oldRate - rate
	}
}

// A networkStruct holds the attributes of one of the model's communication subnetworks
type networkStruct struct {
	Name        string        // unique name
	Groups      []string      // list of groups to which network belongs
	Number      int           // unique integer id
	NetScale    NetworkScale  // type, e.g., LAN, WAN, etc.
	NetMedia    NetworkMedia  // communication fabric, e.g., wired, wireless
	NetRouters  []*routerDev  // list of pointers to routerDevs with interfaces that face this subnetwork
	NetSwitches []*switchDev  // list of pointers to routerDevs with interfaces that face this subnetwork
	NetEndpts   []*endptDev   // list of pointers to routerDevs with interfaces that face this subnetwork
	NetState    *networkState // pointer to a block of information comprising the network 'state'
}

// A networkState struct holds some static and dynamic information about the network's current state
type networkState struct {
	Latency  float64 // latency through network (without considering explicitly declared wired connections) under no load
	Bndwdth  float64 //
	Capacity float64 // maximum traffic capacity of network
	Trace    bool    // switch for calling trace saving
	Drop     bool    // switch for dropping packets with random sampling
	Rngstrm  *rngstream.RngStream

	ClassBndwdth map[int]float64 // map of reservation ID to reserved bandwidth

	// revisit
	Flows   map[int]ActiveRec
	Forward map[int]map[intrfcIDPair]float64

	Load    float64 // real-time value of total load (in units of Mbytes/sec)
	Packets int     // number of packets actively passing in network
}

// AddFlow updates a networkStruct's data structures to add
// a major flow
func (ns *networkStruct) AddFlow(flowID int, ifcpr intrfcIDPair) {
	_, present := ns.NetState.Flows[flowID]
	if !present {
		ns.NetState.Flows[flowID] = ActiveRec{Number: 0, Rate: 0.0}
	}
	ar := ns.NetState.Flows[flowID]
	ar.Number += 1
	ns.NetState.Flows[flowID] = ar
	ns.NetState.Forward[flowID] = make(map[intrfcIDPair]float64)
	ns.NetState.Forward[flowID][ifcpr] = 0.0
}

// RmFlow updates a networkStruct's data structures to reflect
// removal of a major flow
func (ns *networkStruct) RmFlow(flowID int, ifcpr intrfcIDPair) {
	rate := ns.NetState.Forward[flowID][ifcpr]
	delete(ns.NetState.Forward[flowID], ifcpr)

	ar := ns.NetState.Flows[flowID]
	ar.Number -= 1
	ar.Rate -= rate
	if ar.Number == 0 {
		delete(ns.NetState.Flows, flowID)
		delete(ns.NetState.Forward, flowID)
	} else {
		ns.NetState.Flows[flowID] = ar
	}
}

// ChgFlowRate updates a networkStruct's data structures to reflect
// a change in the requested flow rate for the named flow
func (ns *networkStruct) ChgFlowRate(flowID int, ifcpr intrfcIDPair, rate float64) {

	// initialize (if needed) the forward entry for this flow
	oldRate, present := ns.NetState.Forward[flowID][ifcpr]
	if !present {
		ns.NetState.Forward[flowID] = make(map[intrfcIDPair]float64)
		oldRate = 0.0
	}

	// compute the change of rate and add the change to the variables
	// that accumulate the rates
	deltaRate := rate - oldRate

	ar := ns.NetState.Flows[flowID]
	ar.Rate += deltaRate
	ns.NetState.Flows[flowID] = ar

	// save the new rate
	ns.NetState.Forward[flowID][ifcpr] = rate
}

// determine whether the network state between the source and destination interfaces is congested,
// which can only happen if the interface bandwidth is larger than the configured bandwidth
// between these endpoints, and is busy enough to overwhelm it
func (ns *networkStruct) IsCongested(srcIntrfc, dstIntrfc *intrfcStruct) bool {

	// gather the fixed bandwdth for the source
	usedBndwdth := math.Min(srcIntrfc.State.Bndwdth, dstIntrfc.State.Bndwdth)

	seenFlows := make(map[int]bool)
	for flwID, rate := range srcIntrfc.State.ThruEgress {
		usedBndwdth += rate
		seenFlows[flwID] = true
	}

	for flwID, rate := range dstIntrfc.State.ToIngress {
		if seenFlows[flwID] {
			continue
		}
		usedBndwdth += rate
	}
	net := srcIntrfc.Faces
	netuseableBW := net.NetState.Bndwdth
	if usedBndwdth < netuseableBW && !(math.Abs(usedBndwdth-netuseableBW) < 1e-3) {
		return false
	}
	return true
}

// initNetworkStruct transforms information from the desc description
// of a network to its networkStruct representation.  This is separated from
// the createNetworkStruct constructor because it requires that the brdcstDmnByName
// and RouterDevByName lists have been created, which in turn requires that
// the router constructors have already been called.  So the call to initNetworkStruct
// is delayed until all of the network device constructors have been called.
func (ns *networkStruct) initNetworkStruct(nd *NetworkDesc) {
	// in NetworkDesc a router is referred to through its string name
	ns.NetRouters = make([]*routerDev, 0)
	for _, rtrName := range nd.Routers {
		// use the router name from the desc representation to find the run-time pointer
		// to the router and append to the network's list
		ns.AddRouter(RouterDevByName[rtrName])
	}

	ns.NetEndpts = make([]*endptDev, 0)
	for _, EndptName := range nd.Endpts {
		// use the router name from the desc representation to find the run-time pointer
		// to the router and append to the network's list
		ns.addEndpt(EndptDevByName[EndptName])
	}

	ns.NetSwitches = make([]*switchDev, 0)
	for _, SwitchName := range nd.Switches {
		// use the router name from the desc representation to find the run-time pointer
		// to the router and append to the network's list
		ns.AddSwitch(SwitchDevByName[SwitchName])
	}
	ns.Groups = nd.Groups
}

// createNetworkStruct is a constructor that initialized some of the features of the networkStruct
// from their expression in a desc representation of the network
func createNetworkStruct(nd *NetworkDesc) *networkStruct {
	ns := new(networkStruct)

	// copy the name
	ns.Name = nd.Name

	ns.Groups = []string{}

	// get a unique integer id locally
	ns.Number = nxtID()

	// get a netScale type from a desc string expression of it
	ns.NetScale = NetScaleFromStr(nd.NetScale)

	// get a netMedia type from a desc string expression of it
	ns.NetMedia = NetMediaFromStr(nd.MediaType)

	// initialize the Router lists, to be filled in by
	// initNetworkStruct after the router constructors are called
	ns.NetRouters = make([]*routerDev, 0)
	ns.NetEndpts = make([]*endptDev, 0)

	// make the state structure, will flesh it out from run-time configuration parameters
	ns.NetState = createNetworkState(ns.Name)
	return ns
}

// createNetworkState constructs the data for a networkState struct
func createNetworkState(name string) *networkState {
	ns := new(networkState)
	ns.Flows = make(map[int]ActiveRec)
	ns.Forward = make(map[int]map[intrfcIDPair]float64)
	ns.ClassBndwdth = make(map[int]float64)
	ns.Packets = 0
	ns.Drop = false
	ns.Rngstrm = rngstream.New(name)
	return ns
}

// NetLatency estimates the time required by a message to traverse the network,
// using the mean time in system of an M/D/1 queue
func (ns *networkStruct) NetLatency(nm *NetworkMsg) float64 {
	// get the rate activity on this channel
	lambda := ns.channelLoad(nm)

	// compute the service mean
	srv := (float64(nm.MsgLen*8) / 1e6) / ns.NetState.Bndwdth

	rho := lambda * srv
	inSys := srv + rho/(2*(1.0/srv)*(1-rho))
	return ns.NetState.Latency + inSys
}

// matchParam is used to determine whether a run-time parameter description
// should be applied to the network. Its definition here helps networkStruct satisfy
// paramObj interface.  To apply or not to apply depends in part on whether the
// attribute given matchParam as input matches what the interface has. The
// interface attributes that can be tested are the media type, and the nework type
func (ns *networkStruct) matchParam(attrbName, attrbValue string) bool {
	switch attrbName {
	case "name":
		return ns.Name == attrbValue
	case "group":
		return slices.Contains(ns.Groups, attrbValue)
	case "media":
		return NetMediaFromStr(attrbValue) == ns.NetMedia
	case "scale":
		return ns.NetScale == NetScaleFromStr(attrbValue)
	}

	// an error really, as we should match only the names given in the switch statement above
	return false
}

// setParam assigns the parameter named in input with the value given in the input.
// N.B. the valueStruct has fields for integer, float, and string values.  Pick the appropriate one.
// setParam's definition here helps networkStruct satisfy the paramObj interface.
func (ns *networkStruct) setParam(paramType string, value valueStruct) {
	// for some attributes we'll want the string-based value, for others the floating point one
	fltValue := value.floatValue

	// branch on the parameter being set
	switch paramType {
	case "latency":
		ns.NetState.Latency = fltValue
	case "bandwidth":
		ns.NetState.Bndwdth = fltValue
	case "capacity":
		ns.NetState.Capacity = fltValue
	case "trace":
		ns.NetState.Trace = value.boolValue
	case "drop":
		ns.NetState.Drop = value.boolValue
	}
}

// paramObjName helps networkStruct satisfy paramObj interface, returns network name
func (ns *networkStruct) paramObjName() string {
	return ns.Name
}

// LogNetEvent saves a log event associated with the network
func (ns *networkStruct) LogNetEvent(time vrtime.Time, nm *NetworkMsg, op string) {
	if !ns.NetState.Trace {
		return
	}
	AddNetTrace(devTraceMgr, time, nm, ns.Number, op)
}

// AddRouter includes the router given as input parameter on the network list of routers that face it
func (ns *networkStruct) AddRouter(newrtr *routerDev) {
	// skip if rtr already exists in network netRouters list
	for _, rtr := range ns.NetRouters {
		if rtr == newrtr || rtr.RouterName == newrtr.RouterName {
			return
		}
	}
	ns.NetRouters = append(ns.NetRouters, newrtr)
}

// addEndpt includes the endpt given as input parameter on the network list of endpts that face it
func (ns *networkStruct) addEndpt(newendpt *endptDev) {
	// skip if endpt already exists in network netEndpts list
	for _, endpt := range ns.NetEndpts {
		if endpt == newendpt || endpt.EndptName == newendpt.EndptName {
			return
		}
	}
	ns.NetEndpts = append(ns.NetEndpts, newendpt)
}

// AddSwitch includes the swtch given as input parameter on the network list of swtchs that face it
func (ns *networkStruct) AddSwitch(newswtch *switchDev) {
	// skip if swtch already exists in network.NetSwitches list
	for _, swtch := range ns.NetSwitches {
		if swtch == newswtch || swtch.SwitchName == newswtch.SwitchName {
			return
		}
	}
	ns.NetSwitches = append(ns.NetSwitches, newswtch)
}

// ServiceRate identifies the rate of the network when viewed as a separated server,
// which means the bndwdth of the channel (possibly implicit) excluding known flows)
func (ns *networkStruct) channelLoad(nm *NetworkMsg) float64 {
	rtStep := (*nm.Route)[nm.StepIdx]
	srcIntrfc := IntrfcByID[rtStep.srcIntrfcID]
	dstIntrfc := IntrfcByID[rtStep.dstIntrfcID]
	return srcIntrfc.State.EgressLambda + dstIntrfc.State.IngressLambda
}

// PcktDrop returns the packet drop bit for the network
func (ns *networkStruct) PcktDrop() bool {
	return ns.NetState.Drop
}

// a endptDev holds information about a endpt
type endptDev struct {
	EndptName       string   // unique name
	EndptGroups     []string // list of groups to which endpt belongs
	EndptModel      string   // model of CPU the endpt uses
	EndptCores      int
	EndptSched      *TaskScheduler            // shares an endpoint's cores among computing tasks
	EndptAccelSched map[string]*TaskScheduler // map of accelerators, indexed by type name
	EndptAccelModel map[string]string         // accel device name on endpoint mapped to device model for timing
	EndptID         int                       // unique integer id
	EndptIntrfcs    []*intrfcStruct           // list of network interfaces embedded in the endpt
	EndptState      *endptState               // a struct holding endpt state
}

// a endptState holds extra informat used by the endpt
type endptState struct {
	Rngstrm *rngstream.RngStream // pointer to a random number generator
	Trace   bool                 // switch for calling add trace
	Drop    bool                 // whether to support packet drops at interface
	Active  map[int]float64
	Load    float64
	Forward DFS
	Packets int

	InterruptDelay float64

	BckgrndRate float64
	BckgrndSrv  float64
	BckgrndIdx  int
}

// matchParam is for other paramObj objects a method for seeing whether
// the device attribute matches the input.  'cept the endptDev is not declared
// to have any such attributes, so this function (included to let endptDev be
// a paramObj) returns false.  Included to allow endptDev to satisfy paramObj interface requirements
func (endpt *endptDev) matchParam(attrbName, attrbValue string) bool {
	switch attrbName {
	case "name":
		return endpt.EndptName == attrbValue
	case "group":
		return slices.Contains(endpt.EndptGroups, attrbValue)
	case "model":
		return endpt.EndptModel == attrbValue
	}

	// an error really, as we should match only the names given in the switch statement above
	return false
}

// setParam gives a value to a endptDev parameter.  The design allows only
// the CPU model parameter to be set, which is allowed here
func (endpt *endptDev) setParam(param string, value valueStruct) {
	switch param {
	case "trace":
		endpt.EndptState.Trace = (value.intValue == 1)
	case "model":
		endpt.EndptModel = value.stringValue
	case "cores":
		endpt.EndptCores = value.intValue
	case "interruptdelay":
		// provided in musecs, so scale
		endpt.EndptState.InterruptDelay = value.floatValue * 1e-6
	case "bckgrndSrv":
		endpt.EndptState.BckgrndSrv = value.floatValue
	case "bckgrndRate":
		endpt.EndptState.BckgrndRate = value.floatValue
	}
}

// paramObjName helps endptDev satisfy paramObj interface, returns the endpt's name
func (endpt *endptDev) paramObjName() string {
	return endpt.EndptName
}

// createEndptDev is a constructor, using information from the desc description of the endpt
func createEndptDev(endptDesc *EndptDesc) *endptDev {
	endpt := new(endptDev)
	endpt.EndptName = endptDesc.Name // unique name
	endpt.EndptModel = endptDesc.Model
	endpt.EndptCores = endptDesc.Cores
	endpt.EndptID = nxtID() // unique integer id, generated at model load-time
	endpt.EndptState = createEndptState(endpt.EndptName)
	endpt.EndptIntrfcs = make([]*intrfcStruct, 0) // initialization of list of interfaces, to be augmented later

	endpt.EndptGroups = make([]string, len(endptDesc.Groups))
	copy(endpt.EndptGroups, endptDesc.Groups)

	endpt.EndptAccelSched = make(map[string]*TaskScheduler)
	endpt.EndptAccelModel = make(map[string]string)

	var accelName string
	accelCores := 1
	for accelCode, accelModel := range endptDesc.Accel {
		// if there is a "," split out the accelerator name and number of cores
		if strings.Contains(accelCode, ",") {
			pieces := strings.Split(accelCode, ",")
			accelName = pieces[0]
			accelCores, _ = strconv.Atoi(pieces[1])
		} else {
			accelName = accelCode
		}
		endpt.EndptAccelModel[accelName] = accelModel
		endpt.EndptAccelSched[accelName] = CreateTaskScheduler(accelCores)
	}
	AccelSchedulersByHostName[endpt.EndptName] = endpt.EndptAccelSched
	return endpt
}

// createEndptState constructs the data for the endpoint state
func createEndptState(name string) *endptState {
	eps := new(endptState)
	eps.Active = make(map[int]float64)
	eps.Load = 0.0
	eps.Packets = 0
	eps.Trace = false
	eps.Forward = make(DFS)
	eps.Rngstrm = rngstream.New(name)

	// default, nothing happens in the background
	eps.BckgrndRate = 0.0
	eps.BckgrndSrv = 0.0
	return eps
}

// initTaskScheduler calls CreateTaskScheduler to create
// the logic for incorporating the impacts of parallel cores
// on execution time
func (endpt *endptDev) initTaskScheduler() {
	scheduler := CreateTaskScheduler(endpt.EndptCores)
	endpt.EndptSched = scheduler
	// remove if already present
	delete(TaskSchedulerByHostName, endpt.EndptName)
	TaskSchedulerByHostName[endpt.EndptName] = scheduler
}

// addIntrfc appends the input intrfcStruct to the list of interfaces embedded in the endpt.
func (endpt *endptDev) addIntrfc(intrfc *intrfcStruct) {
	endpt.EndptIntrfcs = append(endpt.EndptIntrfcs, intrfc)
}

// CPUModel returns the string type description of the CPU model running the endpt
func (endpt *endptDev) CPUModel() string {
	return endpt.EndptModel
}

// DevName returns the endpt name, as part of the TopoDev interface
func (endpt *endptDev) DevName() string {
	return endpt.EndptName
}

// DevID returns the endpt integer id, as part of the TopoDev interface
func (endpt *endptDev) DevID() int {
	return endpt.EndptID
}

// DevType returns the endpt's device type, as part of the TopoDev interface
func (endpt *endptDev) DevType() DevCode {
	return EndptCode
}

// DevModel returns the endpt's model, as part of the TopoDev interface
func (endpt *endptDev) DevModel() string {
	return endpt.EndptModel
}

// DevIntrfcs returns the endpt's list of interfaces, as part of the TopoDev interface
func (endpt *endptDev) DevIntrfcs() []*intrfcStruct {
	return endpt.EndptIntrfcs
}

// DevState returns the endpt's state struct, as part of the TopoDev interface
func (endpt *endptDev) DevState() any {
	return endpt.EndptState
}

func (endpt *endptDev) DevForward() DFS {
	return endpt.EndptState.Forward
}

// devRng returns the endpt's rng pointer, as part of the TopoDev interface
func (endpt *endptDev) DevRng() *rngstream.RngStream {
	return endpt.EndptState.Rngstrm
}

func (endpt *endptDev) LogNetEvent(time vrtime.Time, nm *NetworkMsg, op string) {
	if !endpt.EndptState.Trace {
		return
	}
	AddNetTrace(devTraceMgr, time, nm, endpt.EndptID, op)
}

// DevAddActive adds an active connection, as part of the TopoDev interface.  Not used for endpts, yet
func (endpt *endptDev) DevAddActive(nme *NetworkMsg) {
	endpt.EndptState.Active[nme.ConnectID] = nme.Rate
}

// DevRmActive removes an active connection, as part of the TopoDev interface.  Not used for endpts, yet
func (endpt *endptDev) DevRmActive(connectID int) {
	delete(endpt.EndptState.Active, connectID)
}

// DevDelay returns the state-dependent delay for passage through the device, as part of the TopoDev interface.
// Not really applicable to endpt, so zero is returned
func (endpt *endptDev) DevDelay(msg *NetworkMsg) float64 {
	return 0.0
}

// The switchDev struct holds information describing a run-time representation of a switch
type switchDev struct {
	SwitchName    string          // unique name
	SwitchGroups  []string        // groups to which the switch may belong
	SwitchModel   string          // model name, used to identify performance characteristics
	SwitchID      int             // unique integer id, generated at model-load time
	SwitchIntrfcs []*intrfcStruct // list of network interfaces embedded in the switch
	SwitchState   *switchState    // pointer to the switch's state struct
}

// The switchState struct holds auxiliary information about the switch
type switchState struct {
	Rngstrm      *rngstream.RngStream // pointer to a random number generator
	Trace        bool                 // switch for calling trace saving
	Drop         bool                 // switch to allow dropping packets
	Active       map[int]float64
	Load         float64
	BufferSize   float64
	Capacity     float64
	Forward      DFS
	Packets      int
	DefaultOp    map[string]string
	DevExecOpTbl map[string]OpMethod
}

// createSwitchDev is a constructor, initializing a run-time representation of a switch from its desc description
func createSwitchDev(switchDesc *SwitchDesc) *switchDev {
	swtch := new(switchDev)
	swtch.SwitchName = switchDesc.Name
	swtch.SwitchModel = switchDesc.Model
	swtch.SwitchID = nxtID()
	swtch.SwitchIntrfcs = make([]*intrfcStruct, 0)
	swtch.SwitchGroups = switchDesc.Groups
	swtch.SwitchState = createSwitchState(switchDesc)
	return swtch
}

// createSwitchState constructs data structures for the switch's state
func createSwitchState(switchDesc *SwitchDesc) *switchState {
	ss := new(switchState)
	ss.Active = make(map[int]float64)
	ss.Load = 0.0
	ss.Packets = 0
	ss.Trace = false
	ss.Drop = false
	ss.Rngstrm = rngstream.New(switchDesc.Name)
	ss.Forward = make(map[int]intrfcIDPair)
	ss.DevExecOpTbl = make(map[string]OpMethod)
	ss.DefaultOp = make(map[string]string)
	for src := range switchDesc.OpDict {
		op := switchDesc.OpDict[src]
		ss.DefaultOp[src] = op

		// use this initialization to test against in DevDelay
		ss.DevExecOpTbl[op] = nil
	}
	return ss
}

// DevForward returns the switch's forward table
func (swtch *switchDev) DevForward() DFS {
	return swtch.SwitchState.Forward
}

// addForward adds an ingress/egress pair to the switch's forwarding table for a flowID
func (swtch *switchDev) addForward(flowID int, idp intrfcIDPair) {
	swtch.SwitchState.Forward[flowID] = idp
}

// rmForward removes a flowID from the switch's forwarding table
func (swtch *switchDev) rmForward(flowID int) {
	delete(swtch.SwitchState.Forward, flowID)
}

func (swtch *switchDev) AddDevExecOp(op string, opFunc OpMethod) {
	swtch.SwitchState.DevExecOpTbl[op] = opFunc
}

func (swtch *switchDev) SetDefaultOp(src, op string) {
	swtch.SwitchState.DefaultOp[src] = op
}

// matchParam is used to determine whether a run-time parameter description
// should be applied to the switch. Its definition here helps switchDev satisfy
// the paramObj interface.  To apply or not to apply depends in part on whether the
// attribute given matchParam as input matches what the switch has. 'model' is the
// only attribute we use to match a switch
func (swtch *switchDev) matchParam(attrbName, attrbValue string) bool {
	switch attrbName {
	case "name":
		return swtch.SwitchName == attrbValue
	case "group":
		return slices.Contains(swtch.SwitchGroups, attrbValue)
	case "model":
		return swtch.SwitchModel == attrbValue
	}

	// an error really, as we should match only the names given in the switch statement above
	return false
}

// setParam gives a value to a switchDev parameter, to help satisfy the paramObj interface.
// Parameters that can be altered on a switch are "model", "execTime", and "buffer"
func (swtch *switchDev) setParam(param string, value valueStruct) {
	switch param {
	case "model":
		swtch.SwitchModel = value.stringValue
	case "buffer":
		swtch.SwitchState.BufferSize = value.floatValue
	case "trace":
		swtch.SwitchState.Trace = value.boolValue
	case "drop":
		swtch.SwitchState.Drop = value.boolValue
	}
}

// paramObjName returns the switch name, to help satisfy the paramObj interface.
func (swtch *switchDev) paramObjName() string {
	return swtch.SwitchName
}

// addIntrfc appends the input intrfcStruct to the list of interfaces embedded in the switch.
func (swtch *switchDev) addIntrfc(intrfc *intrfcStruct) {
	swtch.SwitchIntrfcs = append(swtch.SwitchIntrfcs, intrfc)
}

// DevName returns the switch name, as part of the TopoDev interface
func (swtch *switchDev) DevName() string {
	return swtch.SwitchName
}

// DevID returns the switch integer id, as part of the TopoDev interface
func (swtch *switchDev) DevID() int {
	return swtch.SwitchID
}

// DevType returns the switch's device type, as part of the TopoDev interface
func (swtch *switchDev) DevType() DevCode {
	return SwitchCode
}

// DevModel returns the endpt's model, as part of the TopoDev interface
func (swtch *switchDev) DevModel() string {
	return swtch.SwitchModel
}

// DevIntrfcs returns the switch's list of interfaces, as part of the TopoDev interface
func (swtch *switchDev) DevIntrfcs() []*intrfcStruct {
	return swtch.SwitchIntrfcs
}

// DevState returns the switch's state struct, as part of the TopoDev interface
func (swtch *switchDev) DevState() any {
	return swtch.SwitchState
}

// DevRng returns the switch's rng pointer, as part of the TopoDev interface
func (swtch *switchDev) DevRng() *rngstream.RngStream {
	return swtch.SwitchState.Rngstrm
}

// DevAddActive adds a connection id to the list of active connections through the switch, as part of the TopoDev interface
func (swtch *switchDev) DevAddActive(nme *NetworkMsg) {
	swtch.SwitchState.Active[nme.ConnectID] = nme.Rate
}

// devRmActive removes a connection id to the list of active connections through the switch, as part of the TopoDev interface
func (swtch *switchDev) DevRmActive(connectID int) {
	delete(swtch.SwitchState.Active, connectID)
}

// devDelay returns the state-dependent delay for passage through the switch, as part of the TopoDev interface.
func (swtch *switchDev) DevDelay(msg *NetworkMsg) float64 {
	// if the switch doesn't do anything non-default just do the default switch
	if len(swtch.SwitchState.DevExecOpTbl) == 0 {
		return DelayThruDevice(swtch.SwitchModel, DefaultSwitchOp, msg.MsgLen)
	}

	// look for a match in the keys of the message metadata dictionary and
	// keys in the switch DevExecOpTbl.  On a match, call the function in the table
	// and pass to it the meta data
	for metaKey := range msg.MetaData {
		opFunc, present := swtch.SwitchState.DevExecOpTbl[metaKey]
		if present {

			// see if the function is actually the empty one, meaning its not there
			if opFunc == nil {
				panic(fmt.Errorf("in switch %s dev op %s lacking user-provided instantiation",
					swtch.SwitchName, metaKey))
			}
			return opFunc(swtch, metaKey, msg)
		}
	}

	// didn't find a match, so see if there is a default operation listed for traffic
	// from the previous device
	msgSrcName := prevDeviceName(msg)
	defaultOp, present := swtch.SwitchState.DefaultOp[msgSrcName]
	if present {
		opFunc := swtch.SwitchState.DevExecOpTbl[defaultOp]
		if opFunc == nil {
			panic(fmt.Errorf("in switch %s dev op %s lacking user-provided instantiation",
				swtch.SwitchName, defaultOp))
		}
		return opFunc(swtch, defaultOp, msg)
	}
	// no user-defined default listed, so use system default
	return DelayThruDevice(swtch.SwitchModel, DefaultSwitchOp, msg.MsgLen)
}

// LogNetEvent satisfies TopoDev interface
func (swtch *switchDev) LogNetEvent(time vrtime.Time, nm *NetworkMsg, op string) {
	if !swtch.SwitchState.Trace {
		return
	}
	AddNetTrace(devTraceMgr, time, nm, swtch.SwitchID, op)
}

// The routerDev struct holds information describing a run-time representation of a router
type routerDev struct {
	RouterName    string          // unique name
	RouterGroups  []string        // list of groups to which the router belongs
	RouterModel   string          // attribute used to identify router performance characteristics
	RouterID      int             // unique integer id assigned at model-load time
	RouterIntrfcs []*intrfcStruct // list of interfaces embedded in the router
	RouterState   *routerState    // pointer to the struct of the routers auxiliary state
}

// The routerState type describes auxiliary information about the router
type routerState struct {
	Rngstrm      *rngstream.RngStream // pointer to a random number generator
	Trace        bool                 // switch for calling trace saving
	Drop         bool                 // switch to allow dropping packets
	Active       map[int]float64
	Load         float64
	Buffer       float64
	Forward      map[int]intrfcIDPair
	Packets      int
	DefaultOp    map[string]string
	DevExecOpTbl map[string]OpMethod
}

// createRouterDev is a constructor, initializing a run-time representation of a router from its desc representation
func createRouterDev(routerDesc *RouterDesc) *routerDev {
	router := new(routerDev)
	router.RouterName = routerDesc.Name
	router.RouterModel = routerDesc.Model
	router.RouterID = nxtID()
	router.RouterIntrfcs = make([]*intrfcStruct, 0)
	router.RouterGroups = routerDesc.Groups
	router.RouterState = createRouterState(routerDesc)
	return router
}

// createRouterState is a constructor, initializing the State dictionary for a router
func createRouterState(routerDesc *RouterDesc) *routerState {
	rs := new(routerState)
	rs.Active = make(map[int]float64)
	rs.Load = 0.0
	rs.Buffer = math.MaxFloat64 / 2.0
	rs.Packets = 0
	rs.Trace = false
	rs.Drop = false
	rs.Rngstrm = rngstream.New(routerDesc.Name)
	rs.Forward = make(map[int]intrfcIDPair)
	rs.DevExecOpTbl = make(map[string]OpMethod)
	rs.DefaultOp = make(map[string]string)
	for src := range routerDesc.OpDict {
		op := routerDesc.OpDict[src]
		rs.DefaultOp[src] = op
		rs.DevExecOpTbl[op] = nil
	}
	return rs
}

// DevForward returns the router's forwarding table
func (router *routerDev) DevForward() DFS {
	return router.RouterState.Forward
}

// addForward adds an flowID entry to the router's forwarding table
func (router *routerDev) addForward(flowID int, idp intrfcIDPair) {
	router.RouterState.Forward[flowID] = idp
}

// rmForward removes a flowID from the router's forwarding table
func (router *routerDev) rmForward(flowID int) {
	delete(router.RouterState.Forward, flowID)
}

func (router *routerDev) AddDevExecOp(op string, opFunc OpMethod) {
	router.RouterState.DevExecOpTbl[op] = opFunc
}

func (router *routerDev) SetDefaultOp(src, op string) {
	router.RouterState.DefaultOp[src] = op
}

// matchParam is used to determine whether a run-time parameter description
// should be applied to the router. Its definition here helps switchDev satisfy
// the paramObj interface.  To apply or not to apply depends in part on whether the
// attribute given matchParam as input matches what the router has. 'model' is the
// only attribute we use to match a router
func (router *routerDev) matchParam(attrbName, attrbValue string) bool {
	switch attrbName {
	case "name":
		return router.RouterName == attrbValue
	case "group":
		return slices.Contains(router.RouterGroups, attrbValue)
	case "model":
		return router.RouterModel == attrbValue
	}

	// an error really, as we should match only the names given in the switch statement above
	return false
}

// setParam gives a value to a routerDev parameter, to help satisfy the paramObj interface.
// Parameters that can be altered on a router are "model", "execTime", and "buffer"
func (router *routerDev) setParam(param string, value valueStruct) {
	switch param {
	case "model":
		router.RouterModel = value.stringValue
	case "buffer":
		router.RouterState.Buffer = value.floatValue
	case "trace":
		router.RouterState.Trace = value.boolValue
	case "drop":
		router.RouterState.Drop = value.boolValue
	}
}

// paramObjName returns the router name, to help satisfy the paramObj interface.
func (router *routerDev) paramObjName() string {
	return router.RouterName
}

// addIntrfc appends the input intrfcStruct to the list of interfaces embedded in the router.
func (router *routerDev) addIntrfc(intrfc *intrfcStruct) {
	router.RouterIntrfcs = append(router.RouterIntrfcs, intrfc)
}

// DevName returns the router name, as part of the TopoDev interface
func (router *routerDev) DevName() string {
	return router.RouterName
}

// DevID returns the switch integer id, as part of the TopoDev interface
func (router *routerDev) DevID() int {
	return router.RouterID
}

// DevType returns the router's device type, as part of the TopoDev interface
func (router *routerDev) DevType() DevCode {
	return RouterCode
}

// DevModel returns the endpt's model, as part of the TopoDev interface
func (router *routerDev) DevModel() string {
	return router.RouterModel
}

// DevIntrfcs returns the routers's list of interfaces, as part of the TopoDev interface
func (router *routerDev) DevIntrfcs() []*intrfcStruct {
	return router.RouterIntrfcs
}

// DevState returns the routers's state struct, as part of the TopoDev interface
func (router *routerDev) DevState() any {
	return router.RouterState
}

// DevRng returns a pointer to the routers's rng struct, as part of the TopoDev interface
func (router *routerDev) DevRng() *rngstream.RngStream {
	return router.RouterState.Rngstrm
}

// LogNetEvent includes a network trace report
func (router *routerDev) LogNetEvent(time vrtime.Time, nm *NetworkMsg, op string) {
	if !router.RouterState.Trace {
		return
	}
	AddNetTrace(devTraceMgr, time, nm, router.RouterID, op)
}

// DevAddActive includes a connectID as part of what is active at the device, as part of the TopoDev interface
func (router *routerDev) DevAddActive(nme *NetworkMsg) {
	router.RouterState.Active[nme.ConnectID] = nme.Rate
}

// DevRmActive removes a connectID as part of what is active at the device, as part of the TopoDev interface
func (router *routerDev) DevRmActive(connectID int) {
	delete(router.RouterState.Active, connectID)
}

func (router *routerDev) DevDelay(msg *NetworkMsg) float64 {
	// if the router doesn't do anything non-default just do the default router
	if len(router.RouterState.DevExecOpTbl) == 0 {
		return DelayThruDevice(router.RouterModel, DefaultRouteOp, msg.MsgLen)
	}

	// look for a match in the keys of the message metadata dictionary and
	// keys in the router DevExecOpTbl.  On a match, call the function in the table
	// and pass to it the meta data
	for metaKey := range msg.MetaData {
		opFunc, present := router.RouterState.DevExecOpTbl[metaKey]
		if present {
			if opFunc == nil {
				panic(fmt.Errorf("in router %s dev op %s lacking user-provided instantiation", router.RouterName, metaKey))
			}
			return opFunc(router, metaKey, msg)
		}
	}

	// didn't find a match, so see if there is a default operation listed for traffic
	// from the previous device
	msgSrcName := prevDeviceName(msg)
	defaultOp, present := router.RouterState.DefaultOp[msgSrcName]
	if present {
		opFunc := router.RouterState.DevExecOpTbl[defaultOp]
		if opFunc == nil {
			panic(fmt.Errorf("in router %s dev op %s lacking user-provided instantiation", router.RouterName, defaultOp))
		}
		return opFunc(router, "", msg)
	}
	// no user-defined default listed, so use system default
	return DelayThruDevice(router.RouterModel, DefaultRouteOp, msg.MsgLen)
}

// The intrfcsToDev struct describes a connection to a device.  Used in route descriptions
type intrfcsToDev struct {
	srcIntrfcID int // id of the interface where the connection starts
	dstIntrfcID int // id of the interface embedded by the target device
	netID       int // id of the network between the src and dst interfaces
	devID       int // id of the device the connection targets
}

var NetworkMsgID int = 1

// The NetworkMsg type creates a wrapper for a message between comp pattern funcs.
// One value (StepIdx) indexes into a list of route steps, so that by incrementing
// we can find 'the next' step in the route.  One value is a pointer to this route list,
// and the final value is a pointe to an inter-func comp pattern message.
type NetworkMsg struct {
	MsgID          int             // ID created message is delivered to network
	MsrID          int             // measure identity ID from message delivered to network
	StepIdx        int             // position within the route from source to destination
	Route          *[]intrfcsToDev // pointer to description of route
	Connection     ConnDesc        // {DiscreteConn, MajorFlowConn, MinorFlowConn}
	ExecID         int
	FlowID         int            // flow id given by app at entry
	ConnectID      int            // connection identifier
	NetMsgType     NetworkMsgType // enum type packet,
	PcktRate       float64        // rate rom source
	Rate           float64        // flow rate if FlowID >0 (meaning a flow)
	Syncd          []int          // IDs of flows with which this message has synchronized
	StartTime      float64        // simuation time when the message entered the network
	PrArrvl        float64        // probablity of arrival
	MsgLen         int            // length of the entire message, in bytes
	PcktIdx        int            // index of packet with msg
	NumPckts       int            // number of packets in the message this is part of
	MetaData       map[string]any // carrier of extra stuff
	Msg            any            // message being carried.
	intrfcArr      float64
	StrmPckt       bool
	prevIntrfcID   int     // ID of interface through which message last passed
	prevIntrfcSend float64 // time at which message left previous interface
}

// carriesPckt returns a boolean indicating whether the message is a packet (verses flow)
func (nm *NetworkMsg) carriesPckt() bool {
	return nm.NetMsgType == PacketType
}

// Pt2ptLatency computes the latency on a point-to-point connection
// between interfaces.  Called when neither interface is attached to a router
func Pt2ptLatency(srcIntrfc, dstIntrfc *intrfcStruct) float64 {
	return math.Max(srcIntrfc.State.Latency, dstIntrfc.State.Latency)
}

// currentIntrfcs returns pointers to the source and destination interfaces whose
// id values are carried in the current step of the route followed by the input argument NetworkMsg.
// If the interfaces are not directly connected but communicate through a network,
// a pointer to that network is returned also
func currentIntrfcs(nm *NetworkMsg) (*intrfcStruct, *intrfcStruct, *networkStruct) {
	srcIntrfcID := (*nm.Route)[nm.StepIdx].srcIntrfcID
	dstIntrfcID := (*nm.Route)[nm.StepIdx].dstIntrfcID

	srcIntrfc := IntrfcByID[srcIntrfcID]
	dstIntrfc := IntrfcByID[dstIntrfcID]

	var ns *networkStruct
	netID := (*nm.Route)[nm.StepIdx].netID
	if netID != -1 {
		ns = NetworkByID[netID]
	} else {
		ns = NetworkByID[commonNetID(srcIntrfc, dstIntrfc)]
	}

	return srcIntrfc, dstIntrfc, ns
}

// transitDelay returns the length of time (in seconds) taken
// by the input argument NetworkMsg to traverse the current step in the route it follows.
// That step may be a point-to-point wired connection, or may be transition through a network
// w/o specification of passage through specific devices. In addition a pointer to the
// network transited (if any) is returned
func transitDelay(nm *NetworkMsg) (float64, *networkStruct) {
	var delay float64

	// recover the interfaces themselves and the network between them, if any
	srcIntrfc, dstIntrfc, net := currentIntrfcs(nm)

	if (srcIntrfc.Cable != nil && dstIntrfc.Cable == nil) ||
		(srcIntrfc.Cable == nil && dstIntrfc.Cable != nil) {
		panic("cabled interface confusion")
	}

	if srcIntrfc.Cable == nil {
		// delay is through network (baseline)
		delay = net.NetLatency(nm)
	} else {
		// delay is across a pt-to-pt line
		delay = Pt2ptLatency(srcIntrfc, dstIntrfc)
	}
	return delay, net
}

var lastEntrance float64
var interdist map[int]int = make(map[int]int)
var NumArrivals int
var Foreground int

var StrmLags int
var FgLags int

// enterEgressIntrfc handles the arrival of a packet to the egress interface side of a device.
// It will have been forwarded from the device if an endpoint, or from the ingress interface
func enterEgressIntrfc(evtMgr *evtm.EventManager, egressIntrfc any, msg any) any {
	// cast context argument to interface
	intrfc := egressIntrfc.(*intrfcStruct)

	// cast data argument to network message. Notice that
	// the entire message is passed, not just a pointer to it.
	nm := msg.(NetworkMsg)

	nowInSecs := roundFloat(evtMgr.CurrentSeconds(), rdigits)

	// record arrival
	intrfc.addTrace("enterEgressIntrfc", &nm, nowInSecs)

	AddIntrfcTrace(devTraceMgr, evtMgr.CurrentTime(), nm.ExecID, nm.MsgID,
		intrfc.Number, "enterEgress", intrfc.State.EgressIntrfcQ.Str())

	// note the addition of a network message to the egress queue.
	// This will cause any scheduling that is needed for passing to
	// the exit step of the egress interface
	newMsg := new(NetworkMsg)
	*(newMsg) = nm
	intrfc.State.EgressIntrfcQ.addNetworkMsg(evtMgr, newMsg)
	return nil
}

// advance has already just been called on the strmSet
func schedNxtMsgToIntrfc(evtMgr *evtm.EventManager, intrfcQ *intrfcQStruct) {
	// bring in next message across egress interface, if present
	currentTime := evtMgr.CurrentSeconds()

	// are there foreground messages waiting to be processed?
	if len(intrfcQ.msgQueue) > 0 {
		if intrfcQ.intrfc.Name == "intrfc@hubWest-rtr" && !intrfcQ.ingress {
			msrArrivals = true
		}

		nxtMsg := intrfcQ.msgQueue[0].nm
		serviceTime := computeServiceTime(nxtMsg.MsgLen, intrfcQ.intrfc.State.Bndwdth)
		advanced, qDelay := intrfcQ.strmQ.queueingDelay(currentTime, nxtMsg.intrfcArr, serviceTime, nxtMsg.prevIntrfcID)

		msrArrivals = false

		if advanced {
			evtMgr.Schedule(intrfcQ, nxtMsg.MsgID, enterIntrfcService, vrtime.SecondsToTime(qDelay))
		} else {
			enterIntrfcService(evtMgr, intrfcQ, nxtMsg.MsgID)
		}
	}
}

// exitEgressIntrfc implements an event handler for the completed departure of a message from an interface.
// It determines the time-through-network and destination of the message, and schedules the recognition
// of the message at the ingress interface
func exitEgressIntrfc(evtMgr *evtm.EventManager, egressIntrfc any, data any) any {
	intrfc := egressIntrfc.(*intrfcStruct)
	intrfc.State.EgressTransit = false
	intrfcQ := intrfc.State.EgressIntrfcQ

	currentTime := evtMgr.CurrentSeconds()

	nm := data.(*NetworkMsg)

	if nm == nil {
		panic(fmt.Errorf("popped empty network message queue"))
	}

	// the time through the interface has already been accounted for.
	// we need to add the time through the network, and schedule delivery
	// of the message to the ingress interface after that delay.
	nxtIntrfc := IntrfcByID[(*nm.Route)[nm.StepIdx].dstIntrfcID]

	// create a vrtime representation of when service is completed that starts now
	// vtDepartTime := vrtime.SecondsToTime(evtMgr.CurrentSeconds()+departSrv)
	vtDepartTime := vrtime.SecondsToTime(currentTime)

	// record that time in logs
	intrfc.PrmDev.LogNetEvent(vtDepartTime, nm, "exit")
	intrfc.LogNetEvent(vtDepartTime, nm, "exit")

	AddIntrfcTrace(devTraceMgr, evtMgr.CurrentTime(), nm.ExecID, nm.MsgID, intrfc.Number,
		"exitEgress", intrfc.State.EgressIntrfcQ.Str())

	// remember that we visited
	intrfc.addTrace("exitEgressIntrfc", nm, currentTime)

	netDelay, net := transitDelay(nm)

	// log the completed departure
	net.LogNetEvent(vtDepartTime, nm, "enter")

	// alignment is for debugging only
	// alignment := alignServiceTime(nxtIntrfc, roundFloat(currentTime+netDelay, rdigits), nm.MsgLen)

	priority := int64(intrfc.Device.DevRng().RandInt(1, 1000000))

	// remember that we passed through this interface
	nm.prevIntrfcID = intrfc.Number

	// pass time of last message from this interface to the destination,
	// as this will be used at next interface for constructing
	// strm arrival times that are synchronized with the foreground packet sends
	evtMgr.Schedule(nxtIntrfc, *nm, enterIngressIntrfc, vrtime.SecondsToTimePri(netDelay, priority))

	schedNxtMsgToIntrfc(evtMgr, intrfcQ)

	// event-handlers are required to return _something_
	return nil
}

// enterIngressIntrfc implements the event-handler for the entry of a frame
// to an interface through which the frame will pass on its way into a device. The
// event time is the arrival of the frame, does not yet include load dependent delays
func enterIngressIntrfc(evtMgr *evtm.EventManager, ingressIntrfc any, msg any) any {
	intrfc := ingressIntrfc.(*intrfcStruct)

	// cast data argument to network message
	nm := msg.(NetworkMsg)

	nowInSecs := evtMgr.CurrentSeconds()

	// record arrival
	intrfc.addTrace("enterIngressIntrfc", &nm, nowInSecs)

	AddIntrfcTrace(devTraceMgr, evtMgr.CurrentTime(), nm.ExecID, nm.MsgID,
		intrfc.Number, "enterInress", intrfc.State.IngressIntrfcQ.Str())

	// note the addition of a network message to the ingress queue.
	// This will cause any scheduling that is needed for passing to
	// the exit step of the ingress interface
	newMsg := new(NetworkMsg)
	*(newMsg) = nm
	intrfc.State.IngressIntrfcQ.addNetworkMsg(evtMgr, newMsg)
	return nil
}

func alignServiceTime(intrfc *intrfcStruct, time float64, msgLen int) float64 {
	serviceTime := roundFloat(float64(msgLen*8)/(intrfc.State.Bndwdth*1e6), rdigits)
	ticks := int(time / serviceTime)
	if math.Abs(float64(ticks)*serviceTime-time) < eqlThrsh {
		return 0.0
	}
	return roundFloat(float64(ticks+1)*serviceTime-time, rdigits)
}

func arriveIngressIntrfc(evtMgr *evtm.EventManager, ingressIntrfc any, msg any) any {
	intrfc := ingressIntrfc.(*intrfcStruct)
	intrfc.State.IngressTransit = false
	intrfcQ := intrfc.State.IngressIntrfcQ
	currentTime := evtMgr.CurrentSeconds()
	nm := msg.(*NetworkMsg)

	// remember that we last passed through this interface
	nm.prevIntrfcID = intrfc.Number

	intrfc.LogNetEvent(evtMgr.CurrentTime(), nm, "enter")
	intrfc.PrmDev.LogNetEvent(evtMgr.CurrentTime(), nm, "enter")
	AddIntrfcTrace(devTraceMgr, evtMgr.CurrentTime(), nm.ExecID, nm.MsgID, intrfc.Number,
		"arriveIngress", intrfc.State.IngressIntrfcQ.Str())
	netDevType := intrfc.Device.DevType()

	// cast data argument to network message
	intrfc.addTrace("arriveIngressIntrfc", nm, currentTime)

	// if there is a message waiting for transmission, bring it
	schedNxtMsgToIntrfc(evtMgr, intrfcQ)

	// if this device is an endpoint, the message gets passed up to it
	device := intrfc.Device
	devCode := device.DevType()

	nmbody := *nm
	if devCode == EndptCode {
		// schedule return into comp pattern system, where requested
		ActivePortal.Depart(evtMgr, device.DevName(), nmbody)
		return nil
	}

	// estimate the probability of dropping the packet on the way out
	if netDevType == RouterCode || netDevType == SwitchCode {
		nxtIntrfc := IntrfcByID[(*nmbody.Route)[nm.StepIdx+1].srcIntrfcID]
		buffer := nxtIntrfc.State.BufferSize                         // buffer size in Mbytes
		N := int(math.Round(buffer * 1e+6 / float64(nmbody.MsgLen))) // buffer length in Mbytes
		lambda := intrfc.State.IngressLambda
		prDrop := estPrDrop(lambda, intrfc.State.Bndwdth, N)
		nmbody.PrArrvl *= (1.0 - prDrop)
	}

	// get the delay through the device
	delay := device.DevDelay(&nmbody)
	delay = roundFloat(delay, rdigits)

	// advance position along route
	nmbody.StepIdx += 1
	nxtIntrfc := IntrfcByID[(*nmbody.Route)[nmbody.StepIdx].srcIntrfcID]

	// statement below is just for debugging
	// alignment := alignServiceTime(nxtIntrfc, roundFloat(currentTime+delay, rdigits), nmbody.MsgLen)
	priority := int64(intrfc.Device.DevRng().RandInt(1, 1000000))
	evtMgr.Schedule(nxtIntrfc, nmbody, enterEgressIntrfc, vrtime.SecondsToTimePri(delay, priority))
	return nil
}

var SumWaits float64
var NumWaits int
var ClientStrm int
var StrmSamples int

// schedule the passage of the first message in the intrfcQStruct's queue
// through the interface, as a function of the message length and the interface bandwidth
func enterIntrfcService(evtMgr *evtm.EventManager, context any, data any) any {
	iqs := context.(*intrfcQStruct)
	ingress := iqs.ingress
	intrfc := iqs.intrfc
	msgID := data.(int)

	if iqs.ingress {
		iqs.intrfc.State.IngressTransit = true
		iqs.intrfc.State.IngressTransitMsgID = msgID
	} else {
		iqs.intrfc.State.EgressTransit = true
		iqs.intrfc.State.EgressTransitMsgID = msgID
	}

	// pop the message off the queue
	var nm *NetworkMsg
	if ingress {
		nm = intrfc.State.IngressIntrfcQ.popNetworkMsg()
	} else {
		nm = intrfc.State.EgressIntrfcQ.popNetworkMsg()
	}

	// compute the delay through the interface at its bandwidth speed
	// nm = iqs.msgQueue[0].nm
	if nm.MsgID != msgID {
		panic(fmt.Errorf("intrfcQ msgQueue ordering problem"))
	}

	msgLenMbits := float64(8*nm.MsgLen) / 1e6
	latency := roundFloat(msgLenMbits/intrfc.State.Bndwdth, rdigits)

	// schedule the message delivery on the 'other side' of the interface.
	// The event handler to schedule depends on whether the passage is egress or ingress
	if ingress {
		priority := int64(intrfc.Device.DevRng().RandInt(1, 1000000))
		evtMgr.Schedule(intrfc, nm, arriveIngressIntrfc, vrtime.SecondsToTimePri(latency, priority))
	} else {
		priority := int64(intrfc.Device.DevRng().RandInt(1, 1000000))
		evtMgr.Schedule(intrfc, nm, exitEgressIntrfc, vrtime.SecondsToTimePri(latency, priority))
	}

	return nil
}

// estMM1NFull estimates the probability that an M/M/1/N queue is full.
// formula needs only the server utilization u, and the number of jobs in system, N
func estMM1NFull(u float64, N int) float64 {
	prFull := 1.0 / float64(N+1)
	if math.Abs(1.0-u) < 1e-3 {
		prFull = (1 - u) * math.Pow(u, float64(N)) / (1 - math.Pow(u, float64(N+1)))
	}
	return prFull
}

// estPrDrop estimates the probability that a packet is dropped passing through a network. From the
// function arguments it computes the server utilization and the number of
// messages that can arrive in the delay time, computes the probability
// of being in the 'full' state and returns that
func estPrDrop(rate, capacity float64, N int) float64 {
	// compute the ratio of arrival to service rate
	u := rate / capacity

	// estimate number of packets that can be served as the
	// number that can be present over the latency time,
	// given the rate and message length.
	//
	// in Mbits a packet is 8 * msgLen bytes / (1e+6 bits/Mbbit) = (8*m/1e+6) Mbits
	//
	// with a bandwidth of rate Mbits/sec, the rate in pckts/sec is
	//
	//        Mbits
	//   rate ------
	//         sec                      rate     pckts
	//  -----------------------  =     ------    ----
	//                 Mbits         (8*m/1e+6)   sec
	//    (8*m/1e+6)  ------
	//                  pckt
	//
	// The number of packets that can be accepted at this rate in a period of L secs is
	//
	//  L * rate
	//  -------- pckts
	//  (8*m/1e+6)
	//
	return estMM1NFull(u, N)
}

// FrameSizeCache holds previously computed minimum frame size along the route
// for a flow whose ID is the index
var FrameSizeCache map[int]int = make(map[int]int)

// FindFrameSize traverses a route and returns the smallest MTU on any
// interface along the way.  This defines the maximum frame size to be
// used on that route.
func FindFrameSize(frameID int, rt *[]intrfcsToDev) int {
	_, present := FrameSizeCache[frameID]
	if present {
		return FrameSizeCache[frameID]
	}
	frameSize := 1560
	for _, step := range *rt {
		srcIntrfc := IntrfcByID[step.srcIntrfcID]
		srcFrameSize := srcIntrfc.State.MTU
		if srcFrameSize > 0 && srcFrameSize < frameSize {
			frameSize = srcFrameSize
		}
		dstIntrfc := IntrfcByID[step.dstIntrfcID]
		dstFrameSize := dstIntrfc.State.MTU
		if dstFrameSize > 0 && dstFrameSize < frameSize {
			frameSize = dstFrameSize
		}
	}
	FrameSizeCache[frameID] = frameSize
	return frameSize
}

func DelayThruDevice(model, op string, msgLen int) float64 {
	_, present := devExecTimeTbl[op]
	if !present {
		panic(fmt.Errorf("dev op timing requested for unknown op %v on model %s", op, model))
	}

	tbl, here := devExecTimeTbl[op][model]
	if !here || len(tbl) == 0 {
		panic(fmt.Errorf("dev op timing requested for unknown model %s executing op %s", model, op))
	}
	x := devOpTimeFromTbl(tbl, op, model, msgLen)

	// b1, b2 = Bndwdth
	// x = ExecTime
	// p = Packet length of measurement
	// t = time through device, exclusive of interface transfer
	//
	//   b (Mbits/sec) * (1e6 bits/Mbit) * (byte/8 bits)  = (b*1e6 /8) bytes/usec
	//    so d = 8/(b*1e6) usec/byte
	//
	//  x = p*(d1+d2) + t
	//
	//  t = x- (p*(d1+d2))
	opDesc := tbl[len(tbl)/2]
	b := opDesc.bndwdth
	if !(b > 0.0) {
		if op == "route" {
			b = DefaultFloat["Router-bndwdth"]
		} else {
			b = DefaultFloat["Switch-bndwdth"]
		}
	}
	d := 8.0 / (b * 1e6)
	p := float64(msgLen)

	// remember that units here are microseconds, and the simulator is working at seconds
	return math.Max((x - 2*p*d), 0.0)
}

var devExecTimeCache = make(map[string]map[string]map[int]float64)

func devOpTimeFromTbl(tbl []opTimeDesc, op, model string, msgLen int) float64 {
	// get the parameters needed for the func execution time lookup
	if isNOP(op) {
		return 0.0
	}

	// check cache
	_, present := devExecTimeCache[op]
	if !present {
		devExecTimeCache[op] = make(map[string]map[int]float64)
	}
	_, present = devExecTimeCache[op][model]
	if !present {
		devExecTimeCache[op][model] = make(map[int]float64)
	}
	value, here := devExecTimeCache[op][model][msgLen]
	if here {
		return value
	}

	// the packetlen is not in the map and not in the cache
	//   if msgLen is zero find the smallest entry in the table
	// and call that the value for pcktlen 0.
	pls := []int{}
	for _, pl := range tbl {
		pls = append(pls, pl.pcktLen)
	}
	sort.Ints(pls)

	if msgLen == 0 {
		value := tbl[0].execTime
		devExecTimeCache[op][model][0] = value
		return value
	}

	//   not in the cache, not in the table.  Estimate based on
	//     pcktLen relative to sorted list of known packet lengths
	//   case len(pls) = 1 --- estimate based on straight line from origin to pls[0]
	//   case: pcktLen < pls[0] and len(pls) > 1 --- use slope between pls[0] and pls[1]
	//   case: pls[0] <= pcktLen < pls[len(pls)-1] --- do a linear interpolation
	//   case: pls[len(pls)-1] < pcktLen and len(pls) > 1 --- use slope between last two points
	if len(pls) == 1 {
		value := float64(msgLen) * tbl[0].execTime / float64(tbl[0].pcktLen)
		devExecTimeCache[op][model][msgLen] = value
		return value
	}

	// there are at least two measurements, find the closest way to estimate
	// linear approximation
	leftIdx := 0
	rightIdx := 1
	if pls[len(pls)-1] < msgLen {
		leftIdx = len(pls) - 2
		rightIdx = leftIdx + 1
	} else {
		if pls[0] <= msgLen && msgLen <= pls[len(pls)-1] {
			for pls[rightIdx] < msgLen {
				rightIdx += 1
			}
			leftIdx = rightIdx - 1
		}
	}
	dely := tbl[rightIdx].execTime - tbl[leftIdx].execTime
	delx := float64(tbl[rightIdx].pcktLen - tbl[leftIdx].pcktLen)
	slope := dely / delx
	intercept := tbl[rightIdx].execTime - slope*float64(tbl[rightIdx].pcktLen)
	value = intercept + slope*float64(msgLen)
	devExecTimeCache[op][model][msgLen] = value
	return value
}

func isNOP(op string) bool {
	return (op == "noop" || op == "NOOP" || op == "NOP" || op == "no-op" || op == "nop")
}

func prevDeviceName(msg *NetworkMsg) string {
	route := msg.Route
	rtStep := (*route)[msg.StepIdx]
	srcIntrfc := IntrfcByID[rtStep.srcIntrfcID]
	return srcIntrfc.Device.DevName()
}
