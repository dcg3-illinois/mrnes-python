package mrnes

// transition.go holds state and code related to the transition of
// traffic between the application layer and the mrnes layer,
// and contains the methods involved in managing the 'flow' representation of traffic
//
import (
	"fmt"
	"github.com/iti/evt/evtm"
	"github.com/iti/evt/vrtime"
	"golang.org/x/exp/slices"
	"math"
)

// ConnType tags traffic as discrete or flow
type ConnType int

const (
	FlowConn ConnType = iota
	DiscreteConn
)

// ConnLatency describes one of three ways that latency is ascribed to
// a source-to-destination connection.  'Zero' ascribes none at all, is instantaneous,
// which is used in defining major flow's to reserve bandwidth.   'Place' means
// that at the time a message arrives to the network, a latency to its destination is
// looked up or computed without simulating packet transit across the network.
// 'Simulate' means the packet is simulated traversing the route, through every interface.
type ConnLatency int

const (
	_                = iota
	Zero ConnLatency = iota
	Place
	Simulate
)

// a rtnRecord saves the event handling function to call when the network simulation
// pushes a message back into the application layer.  Characteristics gathered through
// the network traversal are included, and so available to the application layer
type rtnRecord struct {
	pckts   int
	prArrvl float64
	rtnFunc evtm.EventHandlerFunction
	rtnCxt  any
}

// RprtRate is the structure of a message that is scheduled for delivery
// as part of a 'Report' made when a flow rate changes or a packet is lost
type RprtRate struct {
	FlowID       int
	MbrID        int
	AcceptedRate float64
	Action       FlowAction
}

// ConnDesc holds characteristics of a connection...the type (discrete or flow),
// the latency (how delay in delivery is ascribed) and in the case of a flow,
// the action (start, end, rate change)
type ConnDesc struct {
	Type    ConnType
	Latency ConnLatency
	Action  FlowAction
}

// RtnDesc holds the context and event handler
// for scheduling a return
type RtnDesc struct {
	Cxt     any
	EvtHdlr evtm.EventHandlerFunction
}

// RtnDescs hold four RtnDesc structures, for four different use scenarios.
// Bundling in a struct makes code that uses them all more readable at the function call interface
type RtnDescs struct {
	Rtn  *RtnDesc
	Src  *RtnDesc
	Dst  *RtnDesc
	Loss *RtnDesc
}

// NetMsgIDs holds four identifies that may be associated with a flow.
// ExecID comes from the application layer and may tie together numbers of communications
// that occur moving application layer messages between endpoints. FlowID
// refer to a flow identity, although the specific value given is created at the application layer
// (as are the flow themselves).   ConnectID is created at the mrnes layer, describes a single source-to-destination
// message transfer
type NetMsgIDs struct {
	ExecID    int // execution id, from application
	FlowID    int // flow id
	ConnectID int // connection id
}

// EnterNetwork is called after the execution from the application layer
// It creates NetworkMsg structs to represent the start and end of the message, and
// schedules their arrival to the egress interface of the message source endpt
//
// Two kinds of traffic may enter the network, Flow and Discrete
// Entries for a flow may establish a new one, modify an existing one, or delete existing ones.
// Messages that notify destinations of these actions may be delivered instantly, may be delivered using
// an estimate of the cross-network latency which depends on queueing network approximations, or may be
// pushed through the network as individual packets, simulated at each network device.
//
// input connType is one of {Flow, Discrete}
//
//	flowAction is one of {Srt, End, Chg}
//	connLatency is one of {Zero, Place, Simulate}
//
// We approximate the time required for a packet to pass through an interface or network is a transition constant plus
// the mean time in an M/D/1 queuing system.  The arrivals are packets whose length is the frame size,
// the deterministic service time D is time required to serve a packet with a server whose bit service rate
// is the bandwidth available at the interface or network to serve (meaning the total capacity minus the
// bandwidth allocations to other flows at that interface or network), and the arrival rate is the accepted
// rate allocated to the flow.
//
// # Description of possible input parameters
//
// | Message         | connType      | flowAction    | connLatency           | flowID
// | --------------- | ------------- | ------------- | --------------------- | ----------------------- |
// | Discrete Packet | DiscreteConn  | N/A           | Zero, Place, Simulate | >0 => embedded          |
// | Flow            | FlowConn      | Srt, Chg, End | Zero, Place, Simulate | flowID>0                |
func (np *NetworkPortal) EnterNetwork(evtMgr *evtm.EventManager, srcDev, dstDev string, msgLen int,
	connDesc *ConnDesc, IDs NetMsgIDs, rtns RtnDescs, requestRate float64, msrID int, strmPckt bool, strmPcktID int, msg any) (int, float64, bool) {

	// pull out the IDs for clarity
	flowID := IDs.FlowID
	connectID := IDs.ConnectID
	execID := IDs.ExecID

	// if connectID>0 make sure that an entry in np.Connections exists
	_, present := np.Connections[connectID]
	if connectID > 0 && !present {
		panic(fmt.Errorf("non-zero connectID offered to EnterNetwork w/o corresponding Connections entry"))
	}

	// is the message about a discrete packet, or a flow?
	isPckt := (connDesc.Type == DiscreteConn)

	// if flowID >0 and flowAction != Srt, make sure that various np data structures that use it for indexing exist
	if !isPckt && connDesc.Action != Srt {
		_, present0 := np.RequestRate[flowID]
		_, present1 := np.AcceptedRate[flowID]
		if !(present0 && present1) {
			panic(fmt.Errorf("flowID>0 presented to EnterNetwork without supporting data structures"))
		}
	}

	// find the route, which needs the endpoint IDs
	srcID := TopoDevByName[srcDev].DevID()
	dstID := TopoDevByName[dstDev].DevID()
	route := findRoute(srcID, dstID)

	// make sure we have a route to use
	if route == nil || len(*route) == 0 {
		panic(fmt.Errorf("unable to find a route %s -> %s", srcDev, dstDev))
	}

	// take the frame size to be the minimum of the message length
	// and the minimum MTU on interfaces between source and destination
	frameSize := FindFrameSize(flowID, route)
	if isPckt && msgLen < frameSize {
		frameSize = msgLen
	}

	// number of frames for a discrete connection may depend on the message length,
	// all other connections have just one frame reporting the change
	numFrames := 1
	if connDesc.Type == DiscreteConn {
		numFrames = msgLen / frameSize
		if msgLen%frameSize > 0 {
			numFrames += 1
		}
	}

	// A packet entry has flowID == 0
	if !isPckt {
		np.RequestRate[flowID] = requestRate
	}

	if !(connectID > 0) {
		// tell the portal about the arrival, passing to it a description of the
		// response to be made, and the number of frames of the same message that
		// need to be received before reporting completion
		connectID = np.Arrive(rtns, numFrames)

		// remember the flowIDs, given the connectionID
		if !isPckt {
			np.Connections[connectID] = flowID
			np.InvConnection[flowID] = connectID
		}
	}

	// Flows and packets are handled differently
	if connDesc.Type == FlowConn {
		accepted := np.FlowEntry(evtMgr, srcDev, dstDev, msgLen, connDesc,
			flowID, connectID, requestRate, route, msg)
		if accepted {
			return connectID, np.AcceptedRate[flowID], true
		} else {
			return connectID, 0.0, false
		}
	}

	// get the interface through which the message passes to get to the network.
	// remember that a route step names the srcIntrfcID as the interface used to get into a network,
	// and the dstIntrfcID as the interface used to ingress the next device
	intrfc := IntrfcByID[(*route)[0].srcIntrfcID]

	// ordinary packet entry; make a message wrapper and push the message at the entry
	// of the endpt's egress interface.  Segment the message into frames and push them individually
	delay := float64(0.0)

	for fmNumber := 0; fmNumber < numFrames; fmNumber++ {
		nm := new(NetworkMsg)
		nm.MsrID = msrID

		if strmPckt {
			nm.MsgID = strmPcktID
		} else {
			nm.MsgID = NetworkMsgID
			NetworkMsgID += 1
		}

		nm.StepIdx = 0
		nm.Route = route
		nm.Rate = 0.0
		nm.Syncd = make([]int, 0)
		nm.PcktRate = math.MaxFloat64 / 4.0
		nm.PrArrvl = 1.0
		nm.StartTime = evtMgr.CurrentSeconds()
		nm.MsgLen = frameSize
		nm.ConnectID = connectID
		nm.FlowID = flowID
		nm.ExecID = execID
		nm.Connection = *connDesc
		nm.PcktIdx = fmNumber
		nm.NumPckts = numFrames
		nm.StrmPckt = strmPckt
		nm.Msg = msg

		nm.MetaData = make(map[string]any)

		// schedule the message's next destination
		np.SendNetMsg(evtMgr, nm, delay)

		// Now long to get through the device to the interface?
		// The delay above should probably measure CPU bandwidth to deliver to the interface,
		// but it is a small number and another parameter to have to deal with, so we just use interface bandwidth
		delay += (float64(frameSize*8) / 1e6) / intrfc.State.Bndwdth
		delay += intrfc.State.Delay
	}
	return connectID, requestRate, true
}

// FlowEntry handles the entry of flows to the network
func (np *NetworkPortal) FlowEntry(evtMgr *evtm.EventManager, srcDev, dstDev string, msgLen int,
	connDesc *ConnDesc, flowID int, connectID int,
	requestRate float64, route *[]intrfcsToDev, msg any) bool {

	// set the network message and flow connection types
	flowAction := connDesc.Action

	// revise the requested rate for the major flow
	np.RequestRate[flowID] = requestRate

	// Setting up the Flow on Srt
	if flowAction == Srt {
		// include a new flow into the network infrastructure.
		// return a structure whose entries are used to estimate latency when requested
		np.LatencyConsts[flowID] = BuildFlow(flowID, route)
	}

	// change the flow rate for the flowID and take note of all
	// the major flows that were recomputed
	chgFlowIDs, established := np.EstablishFlowRate(evtMgr, flowID, requestRate, route, flowAction)

	if !established {
		return false
	}

	// create the network message to be introduced into the network.
	//
	nm := NetworkMsg{Route: route, Rate: np.AcceptedRate[flowID], PcktRate: math.MaxFloat64 / 4.0,
		PrArrvl: 1.0, MsgLen: msgLen, Connection: *connDesc, ConnectID: connectID, FlowID: flowID,
		Msg: msg, NumPckts: 1, StartTime: evtMgr.CurrentSeconds()}

	// depending on the connLatency we post a message immediately,
	// after an approximated delay, or through simulation

	latency := np.ComputeFlowLatency(&nm)

	np.SendNetMsg(evtMgr, &nm, 0.0)

	// if this is End, remove the identified flow
	if flowAction == End {
		np.RmFlow(evtMgr, flowID, route, latency)
	}

	// for each changed flow report back the change and the acception rate, if requested
	for flwID := range chgFlowIDs {
		// probably not needed but cheap protection against changes in EstablishFlowRate
		if flwID == flowID {
			continue
		}
		np.ReportFlowChg(evtMgr, flwID, flowAction, latency)
	}
	return true
}

// ReportFlowChg visits the return record maps to see if the named flow
// asked to have changes reported, and if so does so as requested.  The reports
// are schedule to occur 'latency' time in the future, when the effect of
// the triggered action is recognized at the triggering flow's receiving end.
func (np *NetworkPortal) ReportFlowChg(evtMgr *evtm.EventManager, flowID int,
	action FlowAction, latency float64) {

	acceptedRate := np.AcceptedRate[flowID]
	rfs := new(RprtRate)
	rfs.FlowID = flowID
	rfs.AcceptedRate = acceptedRate
	rfs.Action = action

	// a request for reporting back to the source is indicated by the presence
	// of an entry in the ReportRtnSrc map
	rrec, present := np.ReportRtnSrc[flowID]
	if present {
		// report the change to the source
		if latency > 0.0 {
			evtMgr.Schedule(rrec.rtnCxt, rfs, rrec.rtnFunc, vrtime.SecondsToTime(latency))
		} else {
			rrec.rtnFunc(evtMgr, rrec.rtnCxt, rfs)
		}
	}

	rrec, present = np.ReportRtnDst[flowID]
	if present {
		// report the change to the destination
		if latency > 0.0 {
			evtMgr.Schedule(rrec.rtnCxt, rfs, rrec.rtnFunc, vrtime.SecondsToTime(latency))
		} else {
			rrec.rtnFunc(evtMgr, rrec.rtnCxt, rfs)
		}
	}
}

// BuildFlow establishes data structures in the interfaces and networks crossed
// by the given route, with a flow having the given flowID.
// No rate information is passed or set, other than initialization
func BuildFlow(flowID int, route *[]intrfcsToDev) float64 {

	// remember the performance coefficients for 'Place' latency, when requested
	var latencyConsts float64

	// for every stop on the route
	for idx := 0; idx < len((*route)); idx++ {

		// remember the step particulars, for later reference
		rtStep := (*route)[idx]

		// rtStep describes a path across a network.
		// the srcIntrfcID is the egress interface on the device that holds
		// that interface. rtStep.netID is the network it faces and
		// devID is the device on the other side.
		//
		egressIntrfc := IntrfcByID[rtStep.srcIntrfcID]
		egressIntrfc.AddFlow(flowID, false)

		// adjust coefficients for embedded packet latency calculation.
		// Add the constant delay through the interface for every frame
		latencyConsts += egressIntrfc.State.Delay

		// if the interface connection is a cable include the interface latency,
		// otherwise view the network step like an interface where
		// queueing occurs
		if egressIntrfc.Cable != nil {
			latencyConsts += egressIntrfc.State.Latency
		} else {
			latencyConsts += egressIntrfc.Faces.NetState.Latency
		}

		// the device gets a Forward entry for this flowID only if the flow doesn't
		// originate there
		if idx > 0 {

			// For idx > 0 we get the dstIntrfcID of (*route)[idx-1] for
			// the ingress interface
			ingressIntrfc := IntrfcByID[(*route)[idx-1].dstIntrfcID]
			ingressIntrfc.AddFlow(flowID, true)

			latencyConsts += ingressIntrfc.State.Delay

			dev := ingressIntrfc.Device

			// a device's forward entry for a flow associates the interface which admits the flow
			// with the interface that exits the flow.
			//   The information needed for such an entry comes from two route steps.
			// With idx>0 and idx < len(*route)-1 we know that the destination of the idx-1 route step
			// is the device ingress, and the source of the current route is the destination
			if idx < len(*route)-1 {
				ip := intrfcIDPair{prevID: ingressIntrfc.Number, nextID: (*route)[idx].srcIntrfcID}

				// remember the connection from ingress to egress interface in the device (router or switch)
				if dev.DevType() == RouterCode {
					rtr := dev.(*routerDev)
					rtr.addForward(flowID, ip)

				} else if dev.DevType() == SwitchCode {
					swtch := dev.(*switchDev)
					swtch.addForward(flowID, ip)
				}
			}
		}

		// remember the connection from ingress to egress interface in the network
		net := NetworkByID[rtStep.netID]
		ifcpr := intrfcIDPair{prevID: rtStep.srcIntrfcID, nextID: rtStep.dstIntrfcID}
		net.AddFlow(flowID, ifcpr)
	}
	return latencyConsts
}

// RmFlow de-establishes data structures in the interfaces and networks crossed
// by the given route, with a flow having the given flowID
func (np *NetworkPortal) RmFlow(evtMgr *evtm.EventManager, rmflowID int,
	route *[]intrfcsToDev, latency float64) {
	var dev TopoDev

	// clear the request rate in case of reference before this call completes
	oldRate := np.RequestRate[rmflowID]

	np.RequestRate[rmflowID] = 0.0

	// remove the flow from the data structures of the interfaces, devices, and networks
	// along the route
	for idx := 0; idx < len((*route)); idx++ {
		rtStep := (*route)[idx]
		var egressIntrfc *intrfcStruct
		var ingressIntrfc *intrfcStruct

		// all steps have an egress side.
		// get the interface
		egressIntrfc = IntrfcByID[rtStep.srcIntrfcID]
		dev = egressIntrfc.Device

		// remove the flow from the interface
		egressIntrfc.RmFlow(rmflowID, oldRate, false)

		// adjust the network to the flow departure
		net := NetworkByID[rtStep.netID]
		ifcpr := intrfcIDPair{prevID: rtStep.srcIntrfcID, nextID: rtStep.dstIntrfcID}
		net.RmFlow(rmflowID, ifcpr)

		// the device got a Forward entry for this flowID only if the flow doesn't
		// originate there
		if idx > 0 {
			ingressIntrfc = IntrfcByID[(*route)[idx-1].dstIntrfcID]
			ingressIntrfc.RmFlow(rmflowID, oldRate, true)

			// remove the flow from the device's forward maps
			switch egressIntrfc.DevType {
			case RouterCode:
				rtr := dev.(*routerDev)
				rtr.rmForward(rmflowID)
			case SwitchCode:
				swtch := dev.(*switchDev)
				swtch.rmForward(rmflowID)
			}
		}
	}

	// report the change to src and dst if requested
	np.ReportFlowChg(evtMgr, rmflowID, End, latency)

	// clear up the maps with indices equal to the ID of the removed flow,
	// and maps indexed by connectionID of the removed flow
	np.ClearRmFlow(rmflowID)
}

// EstablishFlowRate is given a major flow ID, request rate, and a route,
// and then first figures out what the accepted rate can be given the current state
// of all the major flows (by calling DiscoverFlowRate).   It follows up
// by calling SetFlowRate to establish that rate through the route for the named flow.
// Because of congestion, it may be that setting the rate may force recalculation of the
// rates for other major flows, and so SetFlowRate returns a map of flows to be
// revisited, and upper bounds on what their accept rates might be.  This leads to
// a recursive call to EstabishFlowRate
func (np *NetworkPortal) EstablishFlowRate(evtMgr *evtm.EventManager, flowID int,
	requestRate float64, route *[]intrfcsToDev, action FlowAction) (map[int]bool, bool) {

	flowIDs := make(map[int]bool)

	acceptRate, found := np.DiscoverFlowRate(flowID, requestRate, route)
	if !found {
		empty := map[int]bool{}
		return empty, false
	}

	// set the rate, and get back a list of ids of major flows whose rates should be recomputed
	changes := np.SetFlowRate(evtMgr, flowID, acceptRate, route, action)

	// we'll keep track of all the flows calculated (or recalculated)
	flowIDs[flowID] = true

	// revisit every flow whose converged rate might be affected by the rate setting in flow flowID
	for nxtID, nxtRate := range changes {
		if nxtID == flowID {
			continue
		}
		moreIDs, established := np.EstablishFlowRate(evtMgr, nxtID,
			math.Min(nxtRate, np.RequestRate[nxtID]), route, action)

		if !established {
			empty := map[int]bool{}
			return empty, false
		}

		flowIDs[nxtID] = true
		for mID := range moreIDs {
			flowIDs[mID] = true
		}
	}
	return flowIDs, true
}

// DiscoverFlowRate is called after the infrastructure for new
// flow with ID flowID is set up, to determine what its rate will be
func (np *NetworkPortal) DiscoverFlowRate(flowID int,
	requestRate float64, route *[]intrfcsToDev) (float64, bool) {

	minRate := requestRate

	// is the requestRate a hard ask (inelastic) or best effort
	isElastic := np.Elastic[flowID]

	// visit each step on the route
	for idx := 0; idx < len((*route)); idx++ {

		rtStep := (*route)[idx]

		// flag indicating whether we need to analyze the ingress side of the route step.
		// The egress side is always analyzed
		doIngressSide := (idx > 0)

		// ingress side first, then egress side
		for sideIdx := 0; sideIdx < 2; sideIdx++ {
			ingressSide := (sideIdx == 0)
			// the analysis looks the same for the ingress and egress sides, so
			// the same code block can be used for it.   Skip a side that is not
			// consistent with the route step
			if ingressSide && !doIngressSide {
				continue
			}

			// set up intrfc and depending on which interface side we're analyzing
			var intrfc *intrfcStruct
			var intrfcMap map[int]float64
			if ingressSide {
				// router steps describe interface pairs across a network,
				// so our ingress interface ID is the destination interface ID
				// of the previous routing step
				intrfc = IntrfcByID[(*route)[idx-1].dstIntrfcID]
				intrfcMap = intrfc.State.ToIngress
			} else {
				intrfc = IntrfcByID[(*route)[idx].srcIntrfcID]
				intrfcMap = intrfc.State.ToEgress
			}

			// usedBndwdth will accumulate the rates of all existing flows, plus the reservation
			usedBndwdth := 0.0

			// fixedBndwdth will accumulate the rates of all inelastic flows, plus the resevation
			fixedBndwdth := 0.0
			for flwID, rate := range intrfcMap {
				usedBndwdth += rate
				if !np.Elastic[flwID] {
					fixedBndwdth += rate
				}
			}

			// freeBndwdth is what is freely available to any flow
			freeBndwdth := intrfc.State.Bndwdth - usedBndwdth

			// useableBndwdth is what is available to an inelastic flow
			useableBndwdth := intrfc.State.Bndwdth - fixedBndwdth

			// can a request for inelastic bandwidth be satisfied at all?
			if !isElastic && useableBndwdth < requestRate {
				// no
				return 0.0, false
			}

			// can the request on the ingress (non network) side be immediately satisfied?
			if ingressSide && minRate <= freeBndwdth {
				// yes
				continue
			}

			// an inelastic flow can just grab what it wants (and we'll figure out the
			// squeeze later). On the egress side we will need to look at the network.
			//    For an elastic flow we may need to squeeze
			if np.Elastic[flowID] {
				toMap := []int{flowID}

				for flwID := range intrfcMap {
					// avoid having flowID in more than once
					if flwID == flowID {
						continue
					}

					if np.Elastic[flwID] {
						toMap = append(toMap, flwID)
					}
				}

				loadFracVec := ActivePortal.requestedLoadFracVec(toMap)

				// elastic flowID can get its share of the freely available bandwidth
				minRate = math.Min(minRate, loadFracVec[0]*freeBndwdth)
			}

			// when focused on the egress side consider the network faced by the interface
			if !ingressSide {
				net := intrfc.Faces

				// get a pointer to the interface on the other side of the network
				nxtIntrfc := IntrfcByID[rtStep.dstIntrfcID]

				// netUsedBndwdth accumulates the bandwidth of all unique flows that
				// leave the egress side or enter the other interface's ingress side
				var netUsedBndwdth float64

				// netFixedBndwdth accumulates the bandwidth of all unique flows that
				// leave the egress side or enter the other interface's ingress side
				var netFixedBndwdth float64

				// create a list of unique flows that leave the egress side or enter the ingress side
				// and gather up the netUsedBndwdth and netFixedBndwdth rates
				netFlows := make(map[int]bool)
				for flwID, rate := range intrfcMap {
					_, present := netFlows[flwID]
					if present {
						continue
					}
					netFlows[flwID] = true
					netUsedBndwdth += rate
					if !np.Elastic[flwID] {
						netFixedBndwdth += rate
					}
				}

				// incorporate the flows on the ingress side
				for flwID, rate := range nxtIntrfc.State.ToIngress {
					_, present := netFlows[flwID]

					// skip if already seen
					if present {
						continue
					}
					netUsedBndwdth += rate
					if !np.Elastic[flwID] {
						netFixedBndwdth += rate
					}
				}

				// netFreeBndwdth is what is freely available to any flow
				netFreeBndwdth := net.NetState.Bndwdth - netUsedBndwdth

				// netUseableBndwdth is what is available to an inelastic flow
				netUseableBndwdth := net.NetState.Bndwdth - netFixedBndwdth

				if netFreeBndwdth <= 0 || netUseableBndwdth <= 0 {
					return 0.0, false
				}

				// admit a flow if its request rate is less than the
				// netUseableBndwdth
				if requestRate <= netUseableBndwdth {
					continue
				} else if !isElastic {
					return 0.0, false
				}

				// admit an elastic flow if all the elastic flows can be squeezed to let it in,
				// but figure out what its squeezed value needs to be
				toMap := []int{flowID}
				for flwID := range netFlows {
					if flwID == flowID {
						continue
					}

					if np.Elastic[flwID] {
						toMap = append(toMap, flwID)
					}
				}
				loadFracVec := ActivePortal.requestedLoadFracVec(toMap)

				// elastic flowID can get its share of the freely available bandwidth
				minRate = math.Min(minRate, loadFracVec[0]*netFreeBndwdth)
			}
		}
	}
	return minRate, true
}

// SetFlowRate sets the accept rate for major flow flowID all along its path,
// and notes the identities of major flows which need attention because this change
// may impact them or other flows they interact with
func (np *NetworkPortal) SetFlowRate(evtMgr *evtm.EventManager, flowID int, acceptRate float64,
	route *[]intrfcsToDev, action FlowAction) map[int]float64 {

	// this is for keeps (for now...)
	np.AcceptedRate[flowID] = acceptRate

	isElastic := np.Elastic[flowID]

	// remember the ID of the major flows whose accepted rates may change
	changes := make(map[int]float64)

	// visit each step on the route
	var prevDstIntrfcID int
	var dstIntrfcID int
	var prevSrcIntrfcID int

	for idx := 0; idx < len((*route)); idx++ {

		// remember the step particulars
		rtStep := (*route)[idx]

		prevDstIntrfcID = dstIntrfcID
		dstIntrfcID = rtStep.dstIntrfcID

		// ifcpr may be needed to index into a map later
		ifcpr := intrfcIDPair{prevID: rtStep.srcIntrfcID, nextID: rtStep.dstIntrfcID}

		// flag indicating whether we need to analyze the ingress side of the route step.
		// The egress side is always analyzed
		doIngressSide := (idx > 0)

		if idx == 0 {
			outIntrfc := IntrfcByID[rtStep.srcIntrfcID]
			outIntrfc.ChgFlowRate(0, flowID, acceptRate, false)
		}

		// ingress side first, then egress side
		for sideIdx := 0; sideIdx < 2; sideIdx++ {
			ingressSide := (sideIdx == 0)
			// the analysis looks the same for the ingress and egress sides, so
			// the same code block can be used for it.   Skip a side that is not
			// consistent with the route step
			if ingressSide && !doIngressSide {
				continue
			}

			// set up intrfc and intrfcMap depending on which interface side we're analyzing
			var intrfc *intrfcStruct
			var intrfcMap map[int]float64
			if ingressSide {
				// router steps describe interface pairs across a network,
				// so our ingress interface ID is the destination interface ID
				// of the previous routing step
				intrfc = IntrfcByID[(*route)[idx-1].dstIntrfcID]
				prevSrcIntrfcID = (*route)[idx-1].srcIntrfcID
				intrfcMap = intrfc.State.ToIngress
			} else {
				intrfc = IntrfcByID[(*route)[idx].srcIntrfcID]
				intrfcMap = intrfc.State.ToEgress
			}

			// if the accept rate hasn't changed coming into this interface,
			// we can skip it
			if math.Abs(acceptRate-intrfcMap[flowID]) < 1e-3 {
				continue
			}

			fixedBndwdth := 0.0
			for flwID, rate := range intrfcMap {
				if !np.Elastic[flwID] {
					fixedBndwdth += rate
				}
			}

			// if the interface wasn't compressing elastic flows before
			// or after the change, its peers aren't needing attention due to this interface
			wasCongested := intrfc.IsCongested(ingressSide)

			if ingressSide {
				intrfc.ChgFlowRate(prevSrcIntrfcID, flowID, acceptRate, ingressSide)
			} else {
				intrfc.ChgFlowRate(prevDstIntrfcID, flowID, acceptRate, ingressSide)
			}

			isCongested := intrfc.IsCongested(ingressSide)

			if wasCongested || isCongested {
				toMap := []int{}
				if isElastic {
					toMap = []int{flowID}
				}

				for flwID := range intrfcMap {
					// avoid having flowID in more than once
					if flwID == flowID {
						continue
					}
					if np.Elastic[flwID] {
						toMap = append(toMap, flwID)
					}
				}

				var rsrvdFracVec []float64
				if len(toMap) > 0 {
					rsrvdFracVec = np.requestedLoadFracVec(toMap)
				}

				for idx, flwID := range toMap {
					if flwID == flowID {
						continue
					}
					rsvdRate := rsrvdFracVec[idx] * (intrfc.State.Bndwdth - fixedBndwdth)

					// remember the least bandwidth upper bound for major flow flwID
					chgRate, present := changes[flwID]
					if present {
						chgRate = math.Min(chgRate, rsvdRate)
						changes[flwID] = chgRate
					} else {
						changes[flwID] = rsvdRate
					}
				}
			}

			// for the egress side consider the network
			if !ingressSide {
				net := intrfc.Faces

				dstIntrfc := IntrfcByID[rtStep.dstIntrfcID]
				net.ChgFlowRate(flowID, ifcpr, acceptRate)

				wasCongested := net.IsCongested(intrfc, dstIntrfc)

				isCongested := net.IsCongested(intrfc, dstIntrfc)

				if wasCongested || isCongested {
					toMap := []int{}
					if isElastic {
						toMap = []int{flowID}
					}

					for flwID := range intrfc.State.ThruEgress {
						if flwID == flowID {
							continue
						}
						if np.Elastic[flwID] {
							toMap = append(toMap, flwID)
						}
					}

					for flwID := range dstIntrfc.State.ToIngress {
						if slices.Contains(toMap, flwID) {
							continue
						}
						if np.Elastic[flwID] {
							toMap = append(toMap, flwID)
						}
					}
					var rsrvdFracVec []float64
					if len(toMap) > 0 {
						rsrvdFracVec = np.requestedLoadFracVec(toMap)
					}

					for idx, flwID := range toMap {
						if flwID == flowID {
							continue
						}
						rsvdRate := rsrvdFracVec[idx] * net.NetState.Bndwdth
						chgRate, present := changes[flwID]
						if present {
							chgRate = math.Min(chgRate, rsvdRate)
							changes[flwID] = chgRate
						} else {
							changes[flwID] = rsvdRate
						}
					}
				}
			}
		}
	}
	return changes
}

// SendNetMsg moves a NetworkMsg, depending on the latency model.
// If 'Zero' the message goes to the destination instantly, with zero network latency modeled
// If 'Place' the message is placed at the destinatin after computing a delay timing through the network
// If 'Simulate' the message is placed at the egress port of the sending device and the message is simulated
// going through the network to its destination
func (np *NetworkPortal) SendNetMsg(evtMgr *evtm.EventManager, nm *NetworkMsg, offset float64) {

	// remember the latency model, and the route
	connLatency := nm.Connection.Latency
	route := nm.Route

	switch connLatency {
	case Zero:
		// the message's position in the route list---the last step
		nm.StepIdx = len(*route) - 1
		np.SendImmediate(evtMgr, nm)
	case Place:
		// the message's position in the route list---the last step
		nm.StepIdx = len(*route) - 1
		np.PlaceNetMsg(evtMgr, nm, offset)
	case Simulate:
		// get the interface at the first step
		intrfc := IntrfcByID[(*route)[0].srcIntrfcID]

		// add alignment only for debugging
		alignment := alignServiceTime(intrfc, roundFloat(evtMgr.CurrentSeconds()+offset, rdigits), nm.MsgLen)

		// schedule exit from first interface after msg passes through
		evtMgr.Schedule(intrfc, *nm, enterEgressIntrfc, vrtime.SecondsToTime(offset+alignment))
	}
}

// SendImmediate schedules the message with zero latency
func (np *NetworkPortal) SendImmediate(evtMgr *evtm.EventManager, nm *NetworkMsg) {

	// schedule exit from final interface after msg passes through
	intrfc := IntrfcByID[(*nm.Route)[len(*nm.Route)-1].dstIntrfcID]
	device := intrfc.Device
	nmbody := *nm
	ActivePortal.Depart(evtMgr, device.DevName(), nmbody)
}

// PlaceNetMsg schedules the receipt of the message some deterministic time in the future,
// without going through the details of the intervening network structure
func (np *NetworkPortal) PlaceNetMsg(evtMgr *evtm.EventManager, nm *NetworkMsg, offset float64) {

	// get the ingress interface at the end of the route
	ingressIntrfcID := (*nm.Route)[len(*nm.Route)-1].dstIntrfcID
	ingressIntrfc := IntrfcByID[ingressIntrfcID]

	// compute the time through the network if simulated _now_ (and with no packets ahead in queue)
	latency := np.ComputeFlowLatency(nm)

	// mark the message to indicate arrival at the destination
	nm.StepIdx = len((*nm.Route)) - 1

	// schedule exit from final interface after msg passes through
	evtMgr.Schedule(ingressIntrfc, *nm, enterIngressIntrfc, vrtime.SecondsToTime(latency+offset))
}

// ComputeFlowLatency approximates the latency from source to destination if compute now,
// with the state of the network frozen and no packets queued up
func (np *NetworkPortal) ComputeFlowLatency(nm *NetworkMsg) float64 {

	latencyType := nm.Connection.Latency
	if latencyType == Zero {
		return 0.0
	}

	// the latency type will be 'Place' if we reach here,
	flowID := nm.FlowID

	route := nm.Route

	frameSize := 1560
	if nm.MsgLen < frameSize {
		frameSize = nm.MsgLen
	}
	msgLen := float64(frameSize*8) / 1e+6

	// initialize latency with all the constants on the path
	latency := np.LatencyConsts[flowID]

	for idx := 0; idx < len((*route)); idx++ {
		rtStep := (*route)[idx]
		srcIntrfc := IntrfcByID[rtStep.srcIntrfcID]
		latency += msgLen / srcIntrfc.State.Bndwdth

		dstIntrfc := IntrfcByID[rtStep.dstIntrfcID]
		latency += msgLen / dstIntrfc.State.Bndwdth

		net := srcIntrfc.Faces
		latency += net.NetLatency(nm)
	}

	return latency
}

func EstMM1Latency(bitRate, rho float64, msgLen int) float64 {
	// mean time in system for M/M/1 queue is
	// 1/(mu - lambda)
	// in units of pckts/sec.
	// Now
	//
	// bitRate/(msgLen*8) = lambda
	//
	// and rho = lambda/mu
	//
	// so mu = lambda/rho
	// and (mu-lambda) = lambda*(1.0/rho - 1.0)
	// and mean time in system is
	//
	// 1.0/(lambda*(1/rho - 1.0))
	//
	if math.Abs(1.0-rho) < 1e-3 {
		// force rho to be 95%
		rho = 0.95
	}
	lambda := bitRate / float64(msgLen)
	denom := lambda * (1.0/rho - 1.0)
	return 1.0 / denom
}

// EstMD1Latency estimates the delay through an M/D/1 queue
func EstMD1Latency(rho float64, msgLen int, bndwdth float64) float64 {
	// mean time in waiting for service in M/D/1 queue is
	//  1/mu +  rho/(2*mu*(1-rho))
	//
	mu := bndwdth / (float64(msgLen*8) / 1e6)
	imu := 1.0 / mu

	if math.Abs(1.0-rho) < 1e-3 {
		// if rho too large, force it to be 99%
		rho = 0.99
	}
	denom := 2 * mu * (1.0 - rho)
	return imu + rho/denom
}
