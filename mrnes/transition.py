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
