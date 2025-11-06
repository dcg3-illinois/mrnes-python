from typing import Dict, List, Optional, Any
from evt_python.evt import evtm
from evt_python.evt import vrtime
import mrnes
import copy
import routes

class Flow:
    def __init__(self):
        self.ExecID: int = 0
        self.FlowID: int = 0
        self.ConnectID: int = 0
        self.Number: int = 0
        self.Name: str = ""
        self.Mode: str = ""
        self.FlowModel: str = ""
        self.Elastic: bool = False
        self.Pckt: bool = False
        self.Src: str = ""
        self.Dst: str = ""
        self.FrameSize: int = 0
        self.SrcID: int = 0
        self.DstID: int = 0
        self.Groups: List[str] = []
        self.RequestedRate: float = 0.0
        self.AcceptedRate: float = 0.0
        self.RtnDesc: RtnDesc = None  # Should be RtnDesc
        self.Suspended: bool = False
        self.Pushes: int = 0
        self.StrmPckts: int = 0
        self.Origin: str = ""

    # matchParam checks if a Flow matches a given attribute name and value
    def match_param(self, attrb_name: str, attrb_value: str) -> bool:
        if attrb_name == "name":
            return self.Name == attrb_value
        elif attrb_name == "group":
            return attrb_value in self.Groups
        elif attrb_name == "srcdev":
            return self.Src == attrb_value
        elif attrb_name == "dstdev":
            return self.Dst == attrb_value
        return False

    # paramObjName returns the name of the Flow
    def param_obj_name(self) -> str:
        return self.Name

    # setParam sets a parameter on the Flow
    def set_param(self, param_type: str, value: Any):
        if param_type == "reqrate":
            self.RequestedRate = value.floatValue
        elif param_type == "mode":
            self.Mode = value.stringValue
            self.Elastic = (value.stringValue == "elastic-flow")
            self.Pckt = value.stringValue in ("packet", "pckt", "pcket")

    # LogNetEvent is a stub for logging network events
    def log_net_event(self, time, msg, desc):
        pass

    # StartFlow is scheduled the beginning of the flow
    def start_flow(self, evt_mgr, rtns) -> bool:
        # self.RtnDesc.Cxt = context
        # self.RtnDesc.EvtHdlr = hdlr
        self.ConnectID = 0  # indicating absence

        # rtnDesc = new(RtnDesc)
        # rtnDesc.Cxt = context
        # rtnDesc.EvtHdlr = hdlr
        
        ActivePortal.Mode[self.FlowID] = self.Mode
        ActivePortal.Elastic[self.FlowID] = self.Elastic
        ActivePortal.Pckt[self.FlowID] = self.Pckt
        
        OK = True
        if not self.Pckt:
            conn_desc = ConnDesc(Type=FlowConn, Latency=Zero, Action=Srt)
            IDs = NetMsgIDs(ExecID=self.ExecID, FlowID=self.FlowID)
            
            self.ConnectID, _, OK = ActivePortal.EnterNetwork(evt_mgr, self.Src, self.Dst, self.FrameSize,
                conn_desc, IDs, rtns, self.RequestedRate, 0, True, 0, None)
        return OK

    def rm_flow(self, evt_mgr: evtm.EventManager, context, hdlr: evtm.EventHandlerFunction):
        self.RtnDesc.Cxt = context
        self.RtnDesc.EvtHdlr = hdlr
        
        conn_desc = ConnDesc(Type=FlowConn, Latency=Zero, Action=End)
        IDs = NetMsgIDs(ExecID=self.ExecID, FlowID=self.FlowID)
        
        rtn_desc = RtnDesc()
        rtn_desc.Cxt = context
        rtn_desc.EvtHdlr = hdlr
        
        rtns = RtnDescs(Rtn=rtn_desc, Src=None, Dst=None, Loss=None)
        
        ActivePortal.EnterNetwork(evt_mgr, self.Src, self.Dst, self.FrameSize, conn_desc, IDs, rtns, 0.0, 0, False, 0, None)

    def change_rate(self, evt_mgr: evtm.EventManager, request_rate: float) -> bool:
        route = find_route(self.SrcID, self.DstID)
        
        msg = NetworkMsg()
        msg.Connection = ConnDesc(Type=FlowConn, Latency=Zero, Action=Chg)
        msg.ExecID = self.ExecID
        msg.FlowID = self.FlowID
        msg.NetMsgType = FlowType
        msg.Rate = request_rate
        msg.MsgLen = self.FrameSize

        success = ActivePortal.FlowEntry(evt_mgr, self.Src, self.Dst, msg.MsgLen, msg.Connection,
            self.FlowID, self.ConnectID, request_rate, route, msg)
        
        return success

# Global FlowList
FlowList: Dict[int, Flow] = {}

# InitFlowList initializes the global FlowList
def init_flow_list():
    global FlowList
    FlowList = {}

# CreateFlow creates a new Flow and adds it to FlowList
def create_flow(name: str, src_dev: str, dst_dev: str, request_rate: float, frame_size: int, mode: str, exec_id: int, groups: List[str]) -> Optional[Flow]:
    if not (request_rate > 0):
        return None
    
    bgf = Flow()
    bgf.RequestedRate = request_rate
    global numberOfFlows
    numberOfFlows += 1
    bgf.FlowID = numberOfFlows
    bgf.Src = src_dev
    bgf.SrcID = EndptDevByName[src_dev].DevID()
    bgf.Dst = dst_dev
    bgf.DstID = EndptDevByName[dst_dev].DevID()
    bgf.ExecID = exec_id
    bgf.Number = mrnes.nxtID()
    bgf.ConnectID = 0  # indicating absence
    bgf.Mode = mode
    bgf.FrameSize = frame_size
    bgf.Name = name
    bgf.Elastic = (mode == "elastic-flow")
    bgf.Pckt = mode in ("packet", "pckt", "pcket")
    bgf.Suspended = False
    bgf.Groups = copy.deepcopy(groups)

    FlowList[bgf.FlowID] = bgf
    return bgf

# bgfPushStrmServiceTimes: for every interface on a flow's route mark the service time for a strm packet.
# this will be used when computing approximated strm packet arrivals
def bgf_push_strm_service_times(evt_mgr, context, data):
    # acquire the flowID
    flow_id = context
    bgf = FlowList[flow_id]
    
    # if the flow is suspended just leave
    if bgf.Suspended:
        return None
    
    # tag a base arrival time for each interface on the route
    route = routes.find_route(bgf.SrcID, bgf.DstID)
    
    for step in range(len(route)):
        
        # get the routing step
        rt_step = route[step]
        
        # get a pointer to the interface pushing out into the network
        intrfc = IntrfcByID[rt_step.srcIntrfcID]
        
        # time for a strm frame to get through the interface
        service_time = 1.0 / (intrfc.State.Bndwdth * 1e6 / (8 * bgf.FrameSize))
        intrfc.State.StrmServiceTime = service_time
        
        intrfc.State.EgressIntrfcQ.streaming = True
        
        # now consider the interface on the other side of the network
        intrfc = IntrfcByID[rt_step.dstIntrfcID]
        service_time = 1.0 / (intrfc.State.Bndwdth * 1e6 / (8 * bgf.FrameSize))
        intrfc.State.StrmServiceTime = service_time
        intrfc.State.IngressIntrfcQ.streaming = True
    
    return None

# bgfPcktArrivals: handles packet arrivals for a flow
def bgf_pckt_arrivals(evt_mgr: evtm.EventManager, context, data):
    # acquire the flowID
    flow_id = context
    bgf = FlowList[flow_id]
    service_time = data
    
    # if the flow is suspended just leave
    if bgf.Suspended:
        return None
    
    if bgf.StrmPckts == 0:
        route = routes.find_route(bgf.SrcID, bgf.DstID)
        origin_name = IntrfcByID[route[0].dstIntrfcID].Name
        bgf.Origin = origin_name.replace("intrfc@", "", 1)
    
    if bgf.Src not in EndptDevByName:
        raise KeyError(f"Source device {bgf.Src} not the name of an endpoint")
    endpt_dev = EndptDevByName[bgf.Src]
    
    rng = endpt_dev.DevRng()
    arrival_rate_pckts = bgf.RequestedRate * 1e6 / (8 * bgf.FrameSize)
    
    pr_accept = arrival_rate_pckts * service_time
    
    # count number of service slots until accepted
    interarrivals = 1
    while True:
        u01 = rng.RandU01()
        if u01 < pr_accept:
            break
        interarrivals += 1

    # schedule the next arrival
    evt_mgr.Schedule(context, data, bgf_pckt_arrivals, vrtime.SecondsToTime(round_float(interarrivals * service_time, rdigits)))
    
    if evt_mgr.CurrentSeconds() > 0:
        
        # enter the network after the first pass through (which happens at time 0.0)
        conn_desc = ConnDesc(Type=DiscreteConn, Latency=Simulate, Action=None)
        IDs = NetMsgIDs(ExecID=bgf.ExecID, FlowID=flow_id)
        
        # indicate where the returning event is to be delivered
		# need something new here
        rtn_desc = RtnDesc()
        rtn_desc.Cxt = None
        
        # indicate what to do if there is a packet loss
        loss_desc = RtnDesc()
        loss_desc.Cxt = None
        
        rtns = RtnDescs(Rtn=rtn_desc, Src=None, Dst=None, Loss=loss_desc)
        
        ActivePortal.EnterNetwork(evt_mgr, bgf.Src, bgf.Dst, bgf.FrameSize,
            conn_desc, IDs, rtns, arrival_rate_pckts, 0, True, bgf.StrmPckts+100, None)
        
        bgf.StrmPckts += 1
    return None

# FlowRateChange: callback for flow rate change events
def flow_rate_change(evt_mgr: evtm.EventManager, cxt, data):
    bgf = cxt  # type: Flow
    rprt = data  # type: RtnMsgStruct
    bgf.AcceptedRate = rprt.Rate
    
    return None

# FlowRemoved: callback for flow removal events
def flow_removed(evt_mgr: evtm.EventManager, cxt, data):
    bgf = cxt  # type: Flow
    del FlowList[bgf.FlowID]
    evt_mgr.Schedule(bgf.RtnDesc.Cxt, data, bgf.RtnDesc.EvtHdlr, vrtime.SecondsToTime(0.0))
    
    return None

# AcceptedFlowRate: callback for accepted flow rate events
def accepted_flow_rate(evt_mgr: evtm.EventManager, context, data):
    bgf = context  # type: Flow
    rprt = data  # type: RtnMsgStruct
    bgf.AcceptedRate = rprt.Rate
    evt_mgr.Schedule(bgf.RtnDesc.Cxt, data, bgf.RtnDesc.EvtHdlr, vrtime.SecondsToTime(0.0))
    return None

# StartFlows: schedules the beginning of all flows
def start_flows(evt_mgr):
    global ActivePortal
    ActivePortal = CreateNetworkPortal()
    
    rtn_desc = RtnDesc()
    rtn_desc.Cxt = None
    rtn_desc.EvtHdlr = report_flow_evt
    rtns = RtnDescs(Rtn=rtn_desc, Src=rtn_desc, Dst=rtn_desc, Loss=None)
    
    for bgf in FlowList.values():
        success = bgf.start_flow(evt_mgr, rtns)
        if not success:
            print("Flow " + bgf.Name + " failed to start")
            bgf.Suspended = True
    
    # push initial flow packets
    for bgf in FlowList.values():
        if bgf.Suspended:
            continue
        route = routes.find_route(bgf.SrcID, bgf.DstID)
        rt_step = route[0]
        src_intrfc = IntrfcByID[rt_step.srcIntrfcID]
        service_time = 1.0 / (src_intrfc.State.Bndwdth * 1e6 / (8 * bgf.FrameSize))
        
        if bgf.Pckt:
            bgf_pckt_arrivals(evt_mgr, bgf.FlowID, service_time)
        else:
            bgf_push_strm_service_times(evt_mgr, bgf.FlowID, service_time)

# StopFlows: suspends all flows
def stop_flows():
    for bgf in FlowList.values():
        bgf.Suspended = True

# ReportFlowEvt: callback for reporting flow events
def report_flow_evt(evt_mgr, context, data):
    print("Flow event")
    return None

