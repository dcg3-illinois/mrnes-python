package mrnes

import (
	"bufio"
	"fmt"
	"github.com/iti/evt/evtm"
	"github.com/iti/evt/vrtime"
	"golang.org/x/exp/slices"
	"os"
	"strings"
)

type Flow struct {
	ExecID        int
	FlowID        int
	ConnectID     int
	Number        int
	Name          string
	Mode          string
	FlowModel     string
	Elastic       bool
	Pckt          bool
	Src           string
	Dst           string
	FrameSize     int
	SrcID         int
	DstID         int
	Groups        []string
	RequestedRate float64
	AcceptedRate  float64
	RtnDesc       RtnDesc
	Suspended     bool
	Pushes        int
	StrmPckts     int
	Origin        string
}

func (bgf *Flow) matchParam(attrbName, attrbValue string) bool {
	switch attrbName {
	case "name":
		return bgf.Name == attrbValue
	case "group":
		return slices.Contains(bgf.Groups, attrbValue)
	case "srcdev":
		return bgf.Src == attrbValue
	case "dstdev":
		return bgf.Dst == attrbValue
	}
	return false
}

func (bgf *Flow) paramObjName() string {
	return bgf.Name
}

func (bgf *Flow) setParam(paramType string, value valueStruct) {
	switch paramType {
	case "reqrate":
		bgf.RequestedRate = value.floatValue
	case "mode":
		bgf.Mode = value.stringValue
		bgf.Elastic = (value.stringValue == "elastic-flow")
		bgf.Pckt = (value.stringValue == "packet") || (value.stringValue == "pckt") || (value.stringValue == "pcket")
	}
}

func (bgf *Flow) LogNetEvent(time vrtime.Time, msg *NetworkMsg, desc string) {
}

var FlowList map[int]*Flow

func InitFlowList() {
	FlowList = make(map[int]*Flow)
}

func CreateFlow(name string, srcDev string, dstDev string,
	requestRate float64, frameSize int, mode string, execID int, groups []string) *Flow {

	if !(requestRate > 0) {
		return nil
	}

	bgf := new(Flow)
	bgf.RequestedRate = requestRate
	numberOfFlows += 1
	bgf.FlowID = numberOfFlows
	bgf.Src = srcDev
	bgf.SrcID = EndptDevByName[srcDev].DevID()
	bgf.Dst = dstDev
	bgf.DstID = EndptDevByName[dstDev].DevID()
	bgf.ExecID = execID
	bgf.Number = nxtID()
	bgf.ConnectID = 0 // indicating absence
	bgf.Mode = mode
	bgf.FrameSize = frameSize
	bgf.Name = name
	bgf.Elastic = (mode == "elastic-flow")
	bgf.Pckt = (mode == "packet") || (mode == "pckt") || (mode == "pcket")
	bgf.Suspended = false
	copy(bgf.Groups, groups)

	FlowList[bgf.FlowID] = bgf
	return bgf
}

/*
func bgfAdvanceStrmQ(evtMgr *evtm.EventManager, context any, data any) any {
	// find me
	iqs := context.(*intrfcQStruct)
	delay := data.(float64)

	skip := (iqs.ingress && iqs.intrfc.State.IngressTransit) || (!iqs.ingress && iqs.intrfc.State.EgressTransit)

	// advance the stream packets up to the current time
	if !skip {
		currentTime := evtMgr.CurrentSeconds()
		iqs.strmQ.advance(currentTime)
	}

	// schedule the next invokation after delay time
	evtMgr.Schedule(context, data, bgfAdvanceStrmQ, vrtime.SecondsToTime(delay))
	return nil
}
*/

// for every interface on a flow's route mark the service time for a strm packet.
// this will be used when computing approximated strm packet arrivals
func bgfPushStrmServiceTimes(evtMgr *evtm.EventManager, context any, data any) any {
	// acquire the flowID
	flowID := context.(int)
	bgf := FlowList[flowID]

	// if the flow is suspended just leave
	if bgf.Suspended {
		return nil
	}

	// tag a base arrival time for each interface on the route
	route := findRoute(bgf.SrcID, bgf.DstID)

	for step := 0; step < len((*route)); step += 1 {

		// get the routing step
		rtStep := (*route)[step]

		// get a pointer to the interface pushing out into the network
		intrfc := IntrfcByID[rtStep.srcIntrfcID]

		// time for a strm frame to get through the interface
		serviceTime := 1.0 / (intrfc.State.Bndwdth * 1e6 / (float64(8 * bgf.FrameSize)))
		intrfc.State.StrmServiceTime = serviceTime

		intrfc.State.EgressIntrfcQ.streaming = true

		// now consider the interface on the other side of the network
		intrfc = IntrfcByID[rtStep.dstIntrfcID]
		serviceTime = 1.0 / (intrfc.State.Bndwdth * 1e6 / (float64(8 * bgf.FrameSize)))
		intrfc.State.StrmServiceTime = serviceTime
		intrfc.State.IngressIntrfcQ.streaming = true
	}
	return nil
}

var originFile map[string]*os.File = make(map[string]*os.File)
var originScanner map[string]*bufio.Scanner = make(map[string]*bufio.Scanner)

var genPckts int

func bgfPcktArrivals(evtMgr *evtm.EventManager, context any, data any) any {
	// acquire the flowID
	flowID := context.(int)
	bgf := FlowList[flowID]
	serviceTime := data.(float64)

	// if the flow is suspended just leave
	if bgf.Suspended {
		return nil
	}

	if bgf.StrmPckts == 0 {
		route := findRoute(bgf.SrcID, bgf.DstID)
		originName := IntrfcByID[(*route)[0].dstIntrfcID].Name
		bgf.Origin = strings.Replace(originName, "intrfc@", "", 1)
	}

	endptDev, present := EndptDevByName[bgf.Src]
	if !present {
		panic(fmt.Errorf("%s not the name of an endpoint", bgf.Src))
	}

	rng := endptDev.DevRng()
	arrivalRatePckts := bgf.RequestedRate * 1e6 / (float64(8 * bgf.FrameSize))

	prAccept := arrivalRatePckts * serviceTime

	// count number of service slots until accepted
	interarrivals := 1
	for {
		u01 := rng.RandU01()
		if u01 < prAccept {
			break
		}
		interarrivals += 1
	}

	/*
		switch bgf.FlowModel {
			case "expon", "exp", "exponential", "geo","geometric":
				u01 := rng.RandU01()
				params := []float64{arrivalRatePckts, serviceTime}
				interarrival = sampleGeoRV(u01, params)
			case "const","constant":
				interarrival = 1.0/arrivalRatePckts
		}
	*/

	// schedule the next arrival
	evtMgr.Schedule(context, data, bgfPcktArrivals, vrtime.SecondsToTime(roundFloat(float64(interarrivals)*serviceTime, rdigits)))

	if evtMgr.CurrentSeconds() > 0 {

		// enter the network after the first pass through (which happens at time 0.0)
		connDesc := ConnDesc{Type: DiscreteConn, Latency: Simulate, Action: None}
		IDs := NetMsgIDs{ExecID: bgf.ExecID, FlowID: flowID}

		// indicate where the returning event is to be delivered
		// need something new here
		rtnDesc := new(RtnDesc)
		rtnDesc.Cxt = nil

		// indicate what to do if there is a packet loss
		lossDesc := new(RtnDesc)
		lossDesc.Cxt = nil

		rtns := RtnDescs{Rtn: rtnDesc, Src: nil, Dst: nil, Loss: lossDesc}

		ActivePortal.EnterNetwork(evtMgr, bgf.Src, bgf.Dst, bgf.FrameSize,
			&connDesc, IDs, rtns, arrivalRatePckts, 0, true, bgf.StrmPckts+100, nil)

		bgf.StrmPckts += 1
	}
	return nil
}

// StartFlow is scheduled the beginning of the flow
func (bgf *Flow) StartFlow(evtMgr *evtm.EventManager, rtns RtnDescs) bool {
	// bgf.RtnDesc.Cxt = context
	// bgf.RtnDesc.EvtHdlr = hdlr
	bgf.ConnectID = 0 // indicating absence

	// rtnDesc := new(RtnDesc)
	// rtnDesc.Cxt = context
	// rtnDesc.EvtHdlr = hdlr

	ActivePortal.Mode[bgf.FlowID] = bgf.Mode
	ActivePortal.Elastic[bgf.FlowID] = bgf.Elastic
	ActivePortal.Pckt[bgf.FlowID] = bgf.Pckt

	OK := true
	if !bgf.Pckt {
		connDesc := ConnDesc{Type: FlowConn, Latency: Zero, Action: Srt}
		IDs := NetMsgIDs{ExecID: bgf.ExecID, FlowID: bgf.FlowID}

		bgf.ConnectID, _, OK = ActivePortal.EnterNetwork(evtMgr, bgf.Src, bgf.Dst, bgf.FrameSize,
			&connDesc, IDs, rtns, bgf.RequestedRate, 0, true, 0, nil)
	}
	return OK
}

func (bgf *Flow) RmFlow(evtMgr *evtm.EventManager, context any, hdlr evtm.EventHandlerFunction) {
	bgf.RtnDesc.Cxt = context
	bgf.RtnDesc.EvtHdlr = hdlr

	connDesc := ConnDesc{Type: FlowConn, Latency: Zero, Action: End}
	IDs := NetMsgIDs{ExecID: bgf.ExecID, FlowID: bgf.FlowID}

	rtnDesc := new(RtnDesc)
	rtnDesc.Cxt = context
	rtnDesc.EvtHdlr = hdlr

	rtns := RtnDescs{Rtn: rtnDesc, Src: nil, Dst: nil, Loss: nil}

	ActivePortal.EnterNetwork(evtMgr, bgf.Src, bgf.Dst, bgf.FrameSize, &connDesc, IDs, rtns, 0.0, 0, false, 0, nil)
}

func (bgf *Flow) ChangeRate(evtMgr *evtm.EventManager, requestRate float64) bool {
	route := findRoute(bgf.SrcID, bgf.DstID)

	msg := new(NetworkMsg)
	msg.Connection = ConnDesc{Type: FlowConn, Latency: Zero, Action: Chg}
	msg.ExecID = bgf.ExecID
	msg.FlowID = bgf.FlowID
	msg.NetMsgType = FlowType
	msg.Rate = requestRate
	msg.MsgLen = bgf.FrameSize

	success := ActivePortal.FlowEntry(evtMgr, bgf.Src, bgf.Dst, msg.MsgLen, &msg.Connection,
		bgf.FlowID, bgf.ConnectID, requestRate, route, msg)

	return success
}

func FlowRateChange(evtMgr *evtm.EventManager, cxt any, data any) any {
	bgf := cxt.(*Flow)
	rprt := data.(*RtnMsgStruct)
	bgf.AcceptedRate = rprt.Rate
	return nil
}

func FlowRemoved(evtMgr *evtm.EventManager, cxt any, data any) any {
	bgf := cxt.(*Flow)
	delete(FlowList, bgf.FlowID)
	evtMgr.Schedule(bgf.RtnDesc.Cxt, data, bgf.RtnDesc.EvtHdlr, vrtime.SecondsToTime(0.0))

	return nil
}

func AcceptedFlowRate(evtMgr *evtm.EventManager, context any, data any) any {
	bgf := context.(*Flow)
	rprt := data.(*RtnMsgStruct)
	bgf.AcceptedRate = rprt.Rate
	evtMgr.Schedule(bgf.RtnDesc.Cxt, data, bgf.RtnDesc.EvtHdlr, vrtime.SecondsToTime(0.0))
	return nil
}

func StartFlows(evtMgr *evtm.EventManager) {
	ActivePortal = CreateNetworkPortal()

	rtnDesc := new(RtnDesc)
	rtnDesc.Cxt = nil
	rtnDesc.EvtHdlr = ReportFlowEvt
	rtns := RtnDescs{Rtn: rtnDesc, Src: rtnDesc, Dst: rtnDesc, Loss: nil}

	for _, bgf := range FlowList {
		success := bgf.StartFlow(evtMgr, rtns)
		if !success {
			fmt.Println("Flow " + bgf.Name + " failed to start")
			bgf.Suspended = true
		}
	}

	// push initial flow packets
	for _, bgf := range FlowList {
		if bgf.Suspended {
			continue
		}

		route := findRoute(bgf.SrcID, bgf.DstID)
		rtStep := (*route)[0]
		srcIntrfc := IntrfcByID[rtStep.srcIntrfcID]
		serviceTime := 1.0 / (srcIntrfc.State.Bndwdth * 1e6 / float64(8*bgf.FrameSize))

		if bgf.Pckt {
			bgfPcktArrivals(evtMgr, bgf.FlowID, serviceTime)
		} else {
			bgfPushStrmServiceTimes(evtMgr, bgf.FlowID, serviceTime)
		}
	}
}

func StopFlows() {
	for _, bgf := range FlowList {
		bgf.Suspended = true
	}
}

func ReportFlowEvt(evtMgr *evtm.EventManager, context any, data any) any {
	fmt.Println("Flow event")
	return nil
}
