from typing import Any, Callable, Optional, List, Dict
from evt_python.evt import evtm
from evt_python.evt import vrtime
import net
import mrnes
import routes
import flow_sim

# ConnType tags traffic as discrete or flow
class ConnType:
    FlowConn = 0
    DiscreteConn = 1

# ConnLatency describes one of three ways that latency is ascribed to
# a source-to-destination connection.  'Zero' ascribes none at all, is instantaneous,
# which is used in defining major flow's to reserve bandwidth.   'Place' means
# that at the time a message arrives to the network, a latency to its destination is
# looked up or computed without simulating packet transit across the network.
# 'Simulate' means the packet is simulated traversing the route, through every interface.
class ConnLatency:
    Zero = 1
    Place = 2
    Simulate = 3

# a rtnRecord saves the event handling function to call when the network simulation
# pushes a message back into the application layer. Characteristics gathered through
# the network traversal are included, and so available to the application layer
class RtnRecord:
    def __init__(self, pckts: int, pr_arrvl: float, rtn_func: evtm.EventHandlerFunction, rtn_cxt: Any):
        self.pckts = pckts
        self.pr_arrvl = pr_arrvl
        self.rtn_func = rtn_func
        self.rtn_cxt = rtn_cxt

# RprtRate is the structure of a message that is scheduled for delivery
# as part of a 'Report' made when a flow rate changes or a packet is lost
class RprtRate:
    def __init__(self, FlowID: int, MbrID: int, AcceptedRate: float, Action: net.FlowAction):
        self.FlowID = FlowID
        self.MbrID = MbrID
        self.AcceptedRate = AcceptedRate
        self.Action = Action

# ConnDesc holds characteristics of a connection...the type (discrete or flow),
# the latency (how delay in delivery is ascribed) and in the case of a flow,
# the action (start, end, rate change)
class ConnDesc:
    def __init__(self, Type: ConnType, Latency: ConnLatency, Action: net.FlowAction):
        self.Type = Type
        self.Latency = Latency
        self.Action = Action

# RtnDesc holds the context and event handler for scheduling a return
class RtnDesc:
    def __init__(self, Cxt: Any, EvtHdlr: evtm.EventHandlerFunction):
        self.Cxt = Cxt
        self.EvtHdlr = EvtHdlr

# RtnDescs hold four RtnDesc structures, for four different use scenarios.
# Bundling in a struct makes code that uses them all more readable at the function call interface
class RtnDescs:
    def __init__(self, Rtn: Optional[RtnDesc], Src: Optional[RtnDesc], Dst: Optional[RtnDesc], Loss: Optional[RtnDesc]):
        self.Rtn = Rtn
        self.Src = Src
        self.Dst = Dst
        self.Loss = Loss

# NetMsgIDs holds four identifies that may be associated with a flow.
# ExecID comes from the application layer and may tie together numbers of communications
# that occur moving application layer messages between endpoints. FlowID
# refer to a flow identity, although the specific value given is created at the application layer
# (as are the flow themselves).   ConnectID is created at the mrnes layer, describes a single source-to-destination
# message transfer
class NetMsgIDs:
    def __init__(self, ExecID: int, FlowID: int, ConnectID: int):
        self.ExecID = ExecID
        self.FlowID = FlowID
        self.ConnectID = ConnectID

# Mock NetworkPortal class for method placement
class NetworkPortal:
    def __init__(self):
        self.Connections: Dict[int, Any] = {}
        self.InvConnection: Dict[int, Any] = {}
        self.RequestRate: Dict[int, float] = {}
        self.AcceptedRate: Dict[int, float] = {}
        self.LatencyConsts: Dict[int, float] = {}
        self.ReportRtnSrc: Dict[int, Any] = {}
        self.ReportRtnDst: Dict[int, Any] = {}

    # EnterNetwork is called after the execution from the application layer
    # It creates NetworkMsg structs to represent the start and end of the message, and
    # schedules their arrival to the egress interface of the message source endpt
    #
    # Two kinds of traffic may enter the network, Flow and Discrete
    # Entries for a flow may establish a new one, modify an existing one, or delete existing ones.
    # Messages that notify destinations of these actions may be delivered instantly, may be delivered using
    # an estimate of the cross-network latency which depends on queueing network approximations, or may be
    # pushed through the network as individual packets, simulated at each network device.
    #
    # input connType is one of {Flow, Discrete}
    # flowAction is one of {Srt, End, Chg}
    # connLatency is one of {Zero, Place, Simulate}
    #
    # We approximate the time required for a packet to pass through an interface or network is a transition constant plus
    # the mean time in an M/D/1 queuing system.  The arrivals are packets whose length is the frame size,
    # the deterministic service time D is time required to serve a packet with a server whose bit service rate
    # is the bandwidth available at the interface or network to serve (meaning the total capacity minus the
    # bandwidth allocations to other flows at that interface or network), and the arrival rate is the accepted
    # rate allocated to the flow.
    # Description of possible input parameters
    #
    # | Message         | connType      | flowAction    | connLatency           | flowID
    # | --------------- | ------------- | ------------- | --------------------- | ----------------------- |
    # | Discrete Packet | DiscreteConn  | N/A           | Zero, Place, Simulate | >0 => embedded          |
    # | Flow            | FlowConn      | Srt, Chg, End | Zero, Place, Simulate | flowID>0                |
    def enter_network(self, evt_mgr, src_dev, dst_dev, msg_len, conn_desc, IDs, rtns, request_rate, msr_id, strm_pckt, strm_pckt_id, msg) -> tuple[int, float, bool]:
        # pull out the IDs for clarity
        flow_id = IDs.FlowID
        connect_id = IDs.ConnectID
        exec_id = IDs.ExecID

        # if connectID>0 make sure that an entry in np.Connections exists
        present = connect_id in self.Connections
        if connect_id > 0 and not present:
            raise Exception("non-zero connectID offered to EnterNetwork w/o corresponding Connections entry")
        
        # is the message about a discrete packet, or a flow?
        is_pckt = (conn_desc.Type == ConnType.DiscreteConn)
        
        # if flowID >0 and flowAction != Srt, make sure that various np data structures that use it for indexing exist
        if not is_pckt and conn_desc.Action != net.FlowAction.Srt:
            present0 = flow_id in self.RequestRate
            present1 = flow_id in self.AcceptedRate
            if not (present0 and present1):
                raise Exception("flowID>0 presented to EnterNetwork without supporting data structures")
        
        # find the route, which needs the endpoint IDs
        src_id = mrnes.TopoDevByName[src_dev].DevID()
        dst_id = mrnes.TopoDevByName[dst_dev].DevID()
        route = routes.find_route(src_id, dst_id)

        # make sure we have a route to use
        if route is None or len(route) == 0:
            raise Exception(f"unable to find a route {src_dev} -> {dst_dev}")
        
        # take the frame size to be the minimum of the message length
        # and the minimum MTU on interfaces between source and destination
        frame_size = net.FindFrameSize(flow_id, route)
        if is_pckt and msg_len < frame_size:
            frame_size = msg_len
        
        # number of frames for a discrete connection may depend on the message length,
        # all other connections have just one frame reporting the change
        num_frames = 1
        if conn_desc.Type == ConnType.DiscreteConn:
            num_frames = msg_len // frame_size
            if msg_len % frame_size > 0:
                num_frames += 1
        
        # A packet entry has flowID == 0
        if not is_pckt:
            self.RequestRate[flow_id] = request_rate
        
        if not (connect_id > 0):
            # tell the portal about the arrival, passing to it a description of the
            # response to be made, and the number of frames of the same message that
            # need to be received before reporting completion
            connect_id = self.arrive(rtns, num_frames)
            
            # remember the flowIDs, given the connectionID
            if not is_pckt:
                self.Connections[connect_id] = flow_id
                self.InvConnection[flow_id] = connect_id
            
        # Flows and packets are handled differently
        if conn_desc.Type == ConnType.FlowConn:
            accepted = self.flow_entry(evt_mgr, src_dev, dst_dev, msg_len, conn_desc, 
                flow_id, connect_id, request_rate, route, msg)
            if accepted:
                return connect_id, self.AcceptedRate[flow_id], True
            else:
                return connect_id, 0.0, False
        
        # get the interface through which the message passes to get to the network.
        # remember that a route step names the srcIntrfcID as the interface used to get into a network,
        # and the dstIntrfcID as the interface used to ingress the next device
        intrfc = mrnes.IntrfcByID[route[0].srcIntrfcID]

        # ordinary packet entry; make a message wrapper and push the message at the entry
        # of the endpt's egress interface.  Segment the message into frames and push them individually
        delay = 0.0

        for fm_number in range(num_frames):
            nm = net.NetworkMsg()
            nm.MsrID = msr_id
            
            if strm_pckt:
                nm.MsgID = strm_pckt_id
            else:
                nm.MsgID = net.NetworkMsgID
                net.NetworkMsgID += 1 # TODO: check to see if this is acutally changing this value
            
            nm.StepIdx = 0
            nm.Route = route
            nm.Rate = 0.0
            nm.Syncd = []
            nm.PcktRate = float('inf') / 4
            nm.PrArrvl = 1.0
            nm.StartTime = evt_mgr.CurrentSeconds()
            nm.MsgLen = frame_size
            nm.ConnectID = connect_id
            nm.FlowID = flow_id
            nm.ExecID = exec_id
            nm.Connection = conn_desc
            nm.PcktIdx = fm_number
            nm.NumPckts = num_frames
            nm.StrmPckt = strm_pckt
            nm.Msg = msg

            nm.MetaData = {}

            # schedule the message's next destination
            self.send_net_msg(evt_mgr, nm, delay)

            # Now long to get through the device to the interface?
            # The delay above should probably measure CPU bandwidth to deliver to the interface,
            # but it is a small number and another parameter to have to deal with, so we just use interface bandwidth
            delay += (frame_size * 8) / 1e6 / intrfc.State.Bndwdth
            delay += intrfc.State.Delay
            
        return connect_id, request_rate, True

    # FlowEntry handles the entry of flows to the network
    def flow_entry(self, evt_mgr, src_dev, dst_dev, msg_len, conn_desc, flow_id, connect_id, request_rate, route, msg):
        # set the network message and flow connection types
        flow_action = conn_desc.Action
        
        # revise the requested rate for the major flow
        self.RequestRate[flow_id] = request_rate
        
        # Setting up the Flow on Srt
        if flow_action == net.FlowAction.Srt:
            # include a new flow into the network infrastructure.
            # return a structure whose entries are used to estimate latency when requested
            self.LatencyConsts[flow_id] = build_flow(flow_id, route)
        
        # change the flow rate for the flowID and take note of all
        # the major flows that were recomputed
        chg_flow_ids, established = self.establish_flow_rate(evt_mgr, flow_id, request_rate, route, flow_action)
        
        if not established:
            return False
        
        # create the network message to be introduced into the network.
        #
        nm = net.NetworkMsg()
        nm.Route = route
        nm.Rate = self.AcceptedRate[flow_id]
        nm.PcktRate = float('inf') / 4
        nm.PrArrvl = 1.0
        nm.MsgLen = msg_len
        nm.Connection = conn_desc
        nm.ConnectID = connect_id
        nm.FlowID = flow_id
        nm.Msg = msg
        nm.NumPckts = 1
        nm.StartTime = evt_mgr.CurrentSeconds()

        # depending on the connLatency we post a message immediately,
        # after an approximated delay, or through simulation
        latency = self.compute_flow_latency(nm)
        
        self.send_net_msg(evt_mgr, nm, 0.0)
        
        # if this is End, remove the identified flow
        if flow_action == net.FlowAction.End:
            self.rm_flow(evt_mgr, flow_id, route, latency)
        
        # for each changed flow report back the change and the acception rate, if requested
        for flw_id in chg_flow_ids:
            # probably not needed but cheap protection against changes in EstablishFlowRate
            if flw_id == flow_id:
                continue
            self.report_flow_chg(evt_mgr, flw_id, flow_action, latency)
        return True

    # ReportFlowChg visits the return record maps to see if the named flow
    # asked to have changes reported, and if so does so as requested.  The reports
    # are schedule to occur 'latency' time in the future, when the effect of
    # the triggered action is recognized at the triggering flow's receiving end.
    def report_flow_chg(self, evt_mgr, flow_id, action, latency):
        accepted_rate = self.AcceptedRate[flow_id]
        rfs = RprtRate()
        rfs.FlowID = flow_id
        rfs.AcceptedRate = accepted_rate
        rfs.Action = action
        
        # a request for reporting back to the source is indicated by the presence
        # of an entry in the ReportRtnSrc map
        rrec = self.ReportRtnSrc.get(flow_id)
        if rrec:
            # report the change to the source
            if latency > 0.0:
                evt_mgr.Schedule(rrec.rtnCxt, rfs, rrec.rtnFunc, vrtime.SecondsToTime(latency))
            else:
                rrec.rtnFunc(evt_mgr, rrec.rtnCxt, rfs)
        
        rrec = self.ReportRtnDst.get(flow_id)
        if rrec:
            # report the change to the destination
            if latency > 0.0:
                evt_mgr.Schedule(rrec.rtnCxt, rfs, rrec.rtnFunc, vrtime.SecondsToTime(latency))
            else:
                rrec.rtnFunc(evt_mgr, rrec.rtnCxt, rfs)
    
    # RmFlow de-establishes data structures in the interfaces and networks crossed
    # by the given route, with a flow having the given flowID
    def rm_flow(self, evt_mgr: evtm.EventManager, rmflow_id: int, route: List[net.intrfcsToDev], latency: float):
        # clear the request rate in case of reference before this call completes
        old_rate = self.RequestRate[rmflow_id]
        self.RequestRate[rmflow_id] = 0.0
        
        # remove the flow from the data structures of the interfaces, devices, and networks
        # along the route
        for idx in range(len(route)):
            rt_step = route[idx]
            
            # all steps have an egress side.
            # get the interface
            egress_intrfc = mrnes.IntrfcByID[rt_step.srcIntrfcID]
            dev = egress_intrfc.Device
            
            # remove the flow from the interface
            egress_intrfc.rm_flow(rmflow_id, old_rate, False)
            
            # adjust the network to the flow departure
            nt = mrnes.NetworkByID[rt_step.netID]
            ifcpr = net.intrfcIDPair(prevID=rt_step.srcIntrfcID, nextID=rt_step.dstIntrfcID)
            nt.rm_flow(rmflow_id, ifcpr)

            # the device got a Forward entry for this flowID only if the flow doesn't originate there
            if idx > 0:
                ingress_intrfc = mrnes.IntrfcByID[route[idx-1].dstIntrfcID]
                ingress_intrfc.rm_flow(rmflow_id, old_rate, True)
                
                # remove the flow from the device's forward maps
                if egress_intrfc.DevType == net.DevCode.RouterCode:
                    rtr = dev  # .as_router()
                    rtr.rm_forward(rmflow_id)
                elif egress_intrfc.DevType == net.DevCode.SwitchCode:
                    swtch = dev  # .as_switch()
                    swtch.rm_forward(rmflow_id)

        # report the change to src and dst if requested
        self.report_flow_chg(evt_mgr, rmflow_id, net.FlowAction.End, latency)
        
        # clear up the maps with indices equal to the ID of the removed flow,
        # and maps indexed by connectionID of the removed flow
        self.clear_rm_flow(rmflow_id)

    # EstablishFlowRate is given a major flow ID, request rate, and a route,
    # and then first figures out what the accepted rate can be given the current state
    # of all the major flows (by calling DiscoverFlowRate).   It follows up
    # by calling SetFlowRate to establish that rate through the route for the named flow.
    # Because of congestion, it may be that setting the rate may force recalculation of the
    # rates for other major flows, and so SetFlowRate returns a map of flows to be
    # revisited, and upper bounds on what their accept rates might be.  This leads to
    # a recursive call to EstablishFlowRate
    def establish_flow_rate(self, evt_mgr: evtm.EventManager, flow_id: int, request_rate: float, route: List[net.intrfcsToDev], action: net.FlowAction):
        flow_ids = {}
        
        accept_rate, found = self.discover_flow_rate(flow_id, request_rate, route)
        if not found:
            return {}, False
        
        # set the rate, and get back a list of ids of major flows whose rates should be recomputed
        changes = self.set_flow_rate(evt_mgr, flow_id, accept_rate, route, action)
        
        # we'll keep track of all the flows calculated (or recalculated)
        flow_ids[flow_id] = True
        
        # revisit every flow whose converged rate might be affected by the rate setting in flow_id
        for nxt_id, nxt_rate in changes.items():
            if nxt_id == flow_id:
                continue
            more_ids, established = self.establish_flow_rate(evt_mgr, nxt_id, min(nxt_rate, self.RequestRate[nxt_id]), route, action)
            
            if not established:
                return {}, False
            
            flow_ids[nxt_id] = True
            for m_id in more_ids:
                flow_ids[m_id] = True
        
        return flow_ids, True

    # DiscoverFlowRate is called after the infrastructure for new
    # flow with ID flowID is set up, to determine what its rate will be
    def discover_flow_rate(self, flow_id: int, request_rate: float, route: List[net.intrfcsToDev]):
        
        min_rate = request_rate
        
        # is the requestRate a hard ask (inelastic) or best effort
        is_elastic = self.Elastic[flow_id]

        # visit each step on the route
        for idx in range(len(route)):
            
            rt_step = route[idx]
            
            # flag indicating whether we need to analyze the ingress side of the route step.
            # The egress side is always analyzed
            do_ingress_side = (idx > 0)
            
            # ingress side first, then egress side
            for side_idx in range(2):
                ingress_side = (side_idx == 0)
                # the analysis looks the same for the ingress and egress sides, so
                # the same code block can be used for it.   Skip a side that is not
                # consistent with the route step
                if ingress_side and not do_ingress_side:
                    continue
                
                # set up intrfc and depending on which interface side we're analyzing
                if ingress_side:
                    # router steps describe interface pairs across a network,
                    # so our ingress interface ID is the destination interface ID
                    # of the previous routing step
                    intrfc = mrnes.IntrfcByID[route[idx-1].dstIntrfcID]
                    intrfc_map = intrfc.State.ToIngress
                else:
                    intrfc = mrnes.IntrfcByID[route[idx].srcIntrfcID]
                    intrfc_map = intrfc.State.ToEgress
                
                # usedBndwdth will accumulate the rates of all existing flows, plus the reservation
                used_bndwdth = 0.0
                
                # fixedBndwdth will accumulate the rates of all inelastic flows, plus the resevation
                fixed_bndwdth = 0.0
                for flw_id, rate in intrfc_map.items():
                    used_bndwdth += rate
                    if not self.Elastic[flw_id]:
                        fixed_bndwdth += rate
                
                # freeBndwdth is what is freely available to any flow
                free_bndwdth = intrfc.State.Bndwdth - used_bndwdth
                
                # useableBndwdth is what is available to an inelastic flow
                useable_bndwdth = intrfc.State.Bndwdth - fixed_bndwdth
                
                # can a request for inelastic bandwidth be satisfied at all?
                if not is_elastic and useable_bndwdth < request_rate:
                    # no
                    return 0.0, False
                
                # can the request on the ingress (non network) side be immediately satisfied?
                if ingress_side and min_rate <= free_bndwdth:
                    # yes
                    continue
                
                # an inelastic flow can just grab what it wants (and we'll figure out the
                # squeeze later). On the egress side we will need to look at the network.
                #    For an elastic flow we may need to squeeze
                if self.Elastic[flow_id]:
                    to_map = [flow_id]
                    
                    for flw_id in intrfc_map:
                        # avoid having flow_id in more than once
                        if flw_id == flow_id:
                            continue
                        
                        if self.Elastic[flw_id]:
                            to_map.append(flw_id)
                    
                    load_frac_vec = net.ActivePortal.requestedLoadFracVec(to_map)
                    
                    # elastic flow_id can get its share of the freely available bandwidth
                    min_rate = min(min_rate, load_frac_vec[0] * free_bndwdth)
                
                # when focused on the egress side consider the network faced by the interface
                if not ingress_side:
                    nt = intrfc.Faces

                    # get a pointer to the interface on the other side of the network
                    nxt_intrfc = mrnes.IntrfcByID[rt_step.dstIntrfcID]
                    
                    # netUsedBndwdth accumulates the bandwidth of all unique flows that
                    # leave the egress side or enter the other interface's ingress side
                    net_used_bndwdth = 0.0
                    
                    # netFixedBndwdth accumulates the bandwidth of all unique flows that
                    # leave the egress side or enter the other interface's ingress side
                    net_fixed_bndwdth = 0.0
                    
                    # create a list of unique flows that leave the egress side or enter the ingress side
                    # and gather up the netUsedBndwdth and netFixedBndwdth rates
                    net_flows = {}
                    for flw_id, rate in intrfc_map.items():
                        if flw_id in net_flows:
                            continue
                        net_flows[flw_id] = True
                        net_used_bndwdth += rate
                        if not self.Elastic[flw_id]:
                            net_fixed_bndwdth += rate
                    
                    # incorporate the flows on the ingress side
                    for flw_id, rate in nxt_intrfc.State.ToIngress.items():
                        if flw_id in net_flows:
                            continue
                        
                        net_used_bndwdth += rate
                        if not self.Elastic[flw_id]:
                            net_fixed_bndwdth += rate
                    
                    # netFreeBndwdth is what is freely available to any flow
                    net_free_bndwdth = nt.NetState.Bndwdth - net_used_bndwdth
                    
                    # netUseableBndwdth is what is available to an inelastic flow
                    net_useable_bndwdth = nt.NetState.Bndwdth - net_fixed_bndwdth
                    
                    if net_free_bndwdth <= 0 or net_useable_bndwdth <= 0:
                        return 0.0, False
                    
                    # admit a flow if its request rate is less than the netUseableBndwdth
                    if request_rate <= net_useable_bndwdth:
                        continue
                    elif not is_elastic:
                        return 0.0, False
                    
                    # admit an elastic flow if all the elastic flows can be squeezed to let it in,
                    # but figure out what its squeezed value needs to be
                    to_map = [flow_id]
                    for flw_id in net_flows:
                        if flw_id == flow_id:
                            continue

                        if self.Elastic[flw_id]:
                            to_map.append(flw_id)
                    load_frac_vec = net.ActivePortal.requestedLoadFracVec(to_map)

                    # elastic flow_id can get its share of the freely available bandwidth
                    min_rate = min(min_rate, load_frac_vec[0] * net_free_bndwdth)
        return min_rate, True
    
    # SetFlowRate sets the accept rate for major flow flowID all along its path,
    # and notes the identities of major flows which need attention because this change
    # may impact them or other flows they interact with
    def set_flow_rate(self, evt_mgr, flow_id, accept_rate, route, action):
        
        # this is for keeps (for now...)
        self.AcceptedRate[flow_id] = accept_rate

        is_elastic = self.Elastic[flow_id]
        
        # remember the ID of the major flows whose accepted rates may change
        changes = {}
        
        prev_dst_intrfc_id = None
        dst_intrfc_id = None
        prev_src_intrfc_id = None
        
        # visit each step on the route
        for idx in range(len(route)):
            # remember the step particulars
            rt_step = route[idx]

            prev_dst_intrfc_id = dst_intrfc_id
            dst_intrfc_id = rt_step.dstIntrfcID

            # ifcpr may be needed to index into a map later
            ifcpr = net.intrfcIDPair(prevID=rt_step.srcIntrfcID, nextID=rt_step.dstIntrfcID)
            
            # flag indicating whether we need to analyze the ingress side of the route step.
            # The egress side is always analyzed
            do_ingress_side = (idx > 0)
            
            if idx == 0:
                out_intrfc = mrnes.IntrfcByID[rt_step.srcIntrfcID]
                out_intrfc.chg_flow_rate(0, flow_id, accept_rate, False)
            
            # ingress side first, then egress side
            for side_idx in range(2):
                ingress_side = (side_idx == 0)
                # the analysis looks the same for the ingress and egress sides, so
                # the same code block can be used for it.   Skip a side that is not
                # consistent with the route step
                if ingress_side and not do_ingress_side:
                    continue
                
                # set up intrfc and intrfcMap depending on which interface side we're analyzing
                if ingress_side:
                    # router steps describe interface pairs across a network,
                    # so our ingress interface ID is the destination interface ID
                    # of the previous routing step
                    intrfc = mrnes.IntrfcByID[route[idx-1].dstIntrfcID]
                    prev_src_intrfc_id = route[idx-1].srcIntrfcID
                    intrfc_map = intrfc.State.ToIngress
                else:
                    intrfc = mrnes.IntrfcByID[route[idx].srcIntrfcID]
                    intrfc_map = intrfc.State.ToEgress
                
                # if the accept rate hasn't changed coming into this interface,
                # we can skip it
                if abs(accept_rate - intrfc_map[flow_id]) < 1e-3:
                    continue
                
                fixed_bndwdth = 0.0
                for flw_id, rate in intrfc_map.items():
                    if not self.Elastic[flw_id]:
                        fixed_bndwdth += rate
                
                # if the interface wasn't compressing elastic flows before
                # or after the change, its peers aren't needing attention due to this interface
                was_congested = intrfc.is_congested(ingress_side)
                if ingress_side:
                    intrfc.chg_flow_rate(prev_src_intrfc_id, flow_id, accept_rate, ingress_side)
                else:
                    intrfc.chg_flow_rate(prev_dst_intrfc_id, flow_id, accept_rate, ingress_side)
                
                is_congested = intrfc.is_congested(ingress_side)
                
                if was_congested or is_congested:
                    to_map = [flow_id] if is_elastic else []
                    
                    for flw_id in intrfc_map:
                        # avoid having flowID in more than once
                        if flw_id == flow_id:
                            continue
                        if self.Elastic[flw_id]:
                            to_map.append(flw_id)
                    
                    rsrvd_frac_vec = self.requestedLoadFracVec(to_map) if to_map else []
                    
                    for idx2, flw_id in enumerate(to_map):
                        if flw_id == flow_id:
                            continue

                        rsvd_rate = rsrvd_frac_vec[idx2] * (intrfc.State.Bndwdth - fixed_bndwdth)
                        
                        # remember the least bandwidth upper bound for major flow flw_id
                        chg_rate = changes.get(flw_id)
                        if chg_rate is not None:
                            chg_rate = min(chg_rate, rsvd_rate)
                            changes[flw_id] = chg_rate
                        else:
                            changes[flw_id] = rsvd_rate

                # for the egress side consider the network
                if not ingress_side:
                    network_obj = intrfc.Faces

                    dst_intrfc = mrnes.IntrfcByID[rt_step.dstIntrfcID]
                    network_obj.chg_flow_rate(flow_id, ifcpr, accept_rate)
                    
                    was_congested = network_obj.is_congested(intrfc, dst_intrfc)
                    
                    is_congested = network_obj.is_congested(intrfc, dst_intrfc)
                    
                    if was_congested or is_congested:
                        to_map = [flow_id] if is_elastic else []
                        
                        for flw_id in intrfc.State.ThruEgress:
                            if flw_id == flow_id:
                                continue
                            if self.Elastic[flw_id]:
                                to_map.append(flw_id)
                        
                        for flw_id in dst_intrfc.State.ToIngress:
                            if flw_id in to_map:
                                continue
                            if self.Elastic[flw_id]:
                                to_map.append(flw_id)
                        
                        rsrvd_frac_vec = self.requestedLoadFracVec(to_map) if to_map else []
                        
                        for idx2, flw_id in enumerate(to_map):
                            if flw_id == flow_id:
                                continue
                            
                            rsvd_rate = rsrvd_frac_vec[idx2] * network_obj.NetState.Bndwdth
                            chg_rate = changes.get(flw_id)
                            if chg_rate is not None:
                                chg_rate = min(chg_rate, rsvd_rate)
                                changes[flw_id] = chg_rate
                            else:
                                changes[flw_id] = rsvd_rate
        return changes

    # SendNetMsg moves a NetworkMsg, depending on the latency model.
    # If 'Zero' the message goes to the destination instantly, with zero network latency modeled
    # If 'Place' the message is placed at the destionation after computing a delay timing through the network
    # If 'Simulate' the message is placed at the egress port of the sending device and the message is simulated
    # going through the network to its destination
    def send_net_msg(self, evt_mgr, nm, offset):
        
        # remember the latency model, and the route
        conn_latency = nm.Connection.Latency
        route = nm.Route
        
        if conn_latency == ConnLatency.Zero:
            # the message's position in the route list---the last step
            nm.StepIdx = len(route) - 1
            self.send_immediate(evt_mgr, nm)
        elif conn_latency == ConnLatency.Place:
            # the message's position in the route list---the last step
            nm.StepIdx = len(route) - 1
            self.place_net_msg(evt_mgr, nm, offset)
        elif conn_latency == ConnLatency.Simulate:
            # get the interface at the first step
            intrfc = mrnes.IntrfcByID[route[0].srcIntrfcID]
            
            # add alignment only for debugging
            alignment = net.align_service_time(intrfc, flow_sim.round_float(evt_mgr.CurrentSeconds() + offset, flow_sim.rdigits), nm.MsgLen)
            
            # schedule exit from first interface after msg passes through
            evt_mgr.Schedule(intrfc, nm, net.enter_egress_intrfc, vrtime.SecondsToTime(offset + alignment))

    # SendImmediate schedules the message with zero latency
    def send_immediate(self, evt_mgr: evtm.EventManager, nm: net.NetworkMsg):
        # schedule exit from final interface after msg passes through
        intrfc = mrnes.IntrfcByID[nm.Route[len(nm.Route)-1].dstIntrfcID]
        device = intrfc.Device
        nmbody = nm
        net.ActivePortal.Depart(evt_mgr, device.DevName(), nmbody)

    # PlaceNetMsg schedules the receipt of the message some deterministic time in the future,
    # without going through the details of the intervening network structure
    def place_net_msg(self, evt_mgr: evtm.EventManager, nm: net.NetworkMsg, offset: float):
        # get the ingress interface at the end of the route
        ingress_intrfc_id = nm.Route[len(nm.Route)-1].dstIntrfcID
        ingress_intrfc = mrnes.IntrfcByID[ingress_intrfc_id]
        
        # compute the time through the network if simulated _now_ (and with no packets ahead in queue)
        latency = self.compute_flow_latency(nm)
        
        # mark the message to indicate arrival at the destination
        nm.StepIdx = len(nm.Route) - 1
        
        # schedule exit from final interface after msg passes through
        evt_mgr.Schedule(ingress_intrfc, nm, net.enter_ingress_intrfc, vrtime.SecondsToTime(latency + offset))

    # ComputeFlowLatency approximates the latency from source to destination if compute now,
    # with the state of the network frozen and no packets queued up
    def compute_flow_latency(self, nm):
        
        latency_type = nm.Connection.Latency
        if latency_type == ConnLatency.Zero:
            return 0.0
        
        # the latency type will be 'Place' if we reach here
        flow_id = nm.FlowID
        
        route = nm.Route
        
        frame_size = 1560
        if nm.MsgLen < frame_size:
            frame_size = nm.MsgLen
        msg_len = float(frame_size * 8) / 1e6

        # initialize latency with all the constants on the path
        latency = self.LatencyConsts[flow_id]

        for idx in range(len(route)):
            rt_step = route[idx]
            src_intrfc = mrnes.IntrfcByID[rt_step.srcIntrfcID]
            latency += msg_len / src_intrfc.State.Bndwdth
            
            dst_intrfc = mrnes.IntrfcByID[rt_step.dstIntrfcID]
            latency += msg_len / dst_intrfc.State.Bndwdth
            
            network_obj = src_intrfc.Faces
            latency += network_obj.NetLatency(nm)
        return latency


# BuildFlow establishes data structures in the interfaces and networks crossed
# by the given route, with a flow having the given flowID.
# No rate information is passed or set, other than initialization
def build_flow(flow_id: int, route: List[net.intrfcsToDev]):
    # remember the performance coefficients for 'Place' latency, when requested
    latency_consts = 0.0
    
    # for every stop on the route
    for idx in range(len(route)):

        # remember the step particulars, for later reference
        rt_step = route[idx]

        # rtStep describes a path across a network.
        # the srcIntrfcID is the egress interface on the device that holds
        # that interface. rtStep.netID is the network it faces and
        # devID is the device on the other side.
        #
        egress_intrfc = mrnes.IntrfcByID[rt_step.srcIntrfcID]
        egress_intrfc.add_flow(flow_id, False)

        # adjust coefficients for embedded packet latency calculation.
        # Add the constant delay through the interface for every frame
        latency_consts += egress_intrfc.State.Delay
        
        # if the interface connection is a cable include the interface latency,
        # otherwise view the network step like an interface where
        # queueing occurs
        if getattr(egress_intrfc, 'Cable', None) is not None:
            latency_consts += egress_intrfc.State.Latency
        else:
            latency_consts += egress_intrfc.Faces.NetState.Latency
        
        # the device gets a Forward entry for this flowID only if the flow doesn't
        # originate there
        if idx > 0:
            
            # For idx > 0 we get the dstIntrfcID of route[idx-1] for
            # the ingress interface
            ingress_intrfc = mrnes.IntrfcByID[route[idx-1].dstIntrfcID]
            ingress_intrfc.add_flow(flow_id, True)

            latency_consts += ingress_intrfc.State.Delay
            
            dev = ingress_intrfc.Device
            
            # a device's forward entry for a flow associates the interface which admits the flow
            # with the interface that exits the flow.
            #   The information needed for such an entry comes from two route steps.
            # With idx>0 and idx < len(route)-1 we know that the destination of the idx-1 route step
            # is the device ingress, and the source of the current route is the destination
            if idx < len(route)-1:
                
                ip = net.intrfcIDPair(prevID=ingress_intrfc.Number, nextID=route[idx].srcIntrfcID)
                
                # remember the connection from ingress to egress interface in the device (router or switch)
                if dev.DevType() == net.DevCode.RouterCode:
                    rtr = dev  # .as_router()
                    rtr.add_forward(flow_id, ip)

                elif dev.DevType() == net.DevCode.SwitchCode:
                    swtch = dev  # .as_switch()
                    swtch.add_forward(flow_id, ip)

        # remember the connection from ingress to egress interface in the network
        nt = mrnes.NetworkByID[rt_step.netID]
        ifcpr = net.intrfcIDPair(prevID=rt_step.srcIntrfcID, nextID=rt_step.dstIntrfcID)
        nt.add_flow(flow_id, ifcpr)

    return latency_consts

def est_mm1_latency(bit_rate: float, rho: float, msg_len: int) -> float:
    # mean time in system for M/M/1 queue is
    # 1/(mu - lambda)
    # in units of pckts/sec.
    # Now
    #
    # bitRate/(msgLen*8) = lambda
    #
    # and rho = lambda/mu
    #
    # so mu = lambda/rho
    # and (mu-lambda) = lambda*(1.0/rho - 1.0)
    # and mean time in system is
    #
    # 1.0/(lambda*(1/rho - 1.0))
    #
    if abs(1.0 - rho) < 1e-3:
        # force rho to be 95%
        rho = 0.95
    lambd = bit_rate / float(msg_len)
    denom = lambd * (1.0 / rho - 1.0)
    return 1.0 / denom

# EstMD1Latency estimates the delay through an M/D/1 queue
def est_md1_latency(rho: float, msg_len: int, bndwdth: float) -> float:
    # mean time in waiting for service in M/D/1 queue is
    #  1/mu +  rho/(2*mu*(1-rho))
    #
    mu = bndwdth / (float(msg_len * 8) / 1e6)
    imu = 1.0 / mu
    
    if abs(1.0 - rho) < 1e-3:
        # if rho too large, force it to be 99%
        rho = 0.99
    denom = 2 * mu * (1.0 - rho)
    return imu + rho / denom
