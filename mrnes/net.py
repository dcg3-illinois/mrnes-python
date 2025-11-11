"""
Python translation of mrnes/net.go
Contains network simulation core data structures and helpers.
All comments from the Go original are preserved.
"""

from typing import List, Dict, Optional, Any, Tuple
from abc import ABC, abstractmethod
import math
from evt_python.evt import evtm
import evt_python.evt.vrtime as vrtime
import mrnes.trace as trace
import mrnes
import net
import routes
import flow_sim
import transition

# The mrnsbit network simulator is built around two strong assumptions that
# simplify implementation, but which may have to be addressed if mrnsbit
# is to be used when fine-grained networking details are thought to be important.
#
# The first assumption is that routing is static, that the complete route for
# a message from specified source to specified destination can be computed at
# the time the message enters the network.   It happens that this implementation
# uses minimum hop count as the metric for choosing routes, but this is not so rigourously
# embedded in the design as is static routes.
#
# The second assumption is related to the reality that messages do not traverse a link or
# pass through an interface instaneously, they 'connect' across links, through networks, and through devices.
# Flows have bandwidth, and every interface, link, device, and network has its own bandwidth
# limit.  From the point of view of the receiving endpt, the effective bandwidth (assuming the bandwidths
# all along the path don't change) is the minimum bandwidth among all the things on the path.
# In the path before the hop that induces the least bandwidth message may scoot through connections
# at higher bandwidth, but the progress of that connection is ultimately limited  by the smallest bandwidth.
# The simplifying assumption then is that the connection of the message's bits _everywhere_ along the path
# is at that minimum bandwidth.   More detail in tracking and changing bandwidths along sections of the message
# are possible, but at this point it is more complicated and time-consuming to do that than it seems to be worth
# for anticipated use-cases.

class intPair:
	"""intPair: holds two integers (i, j)"""
	def __init__(self, i: int, j: int):
		self.i = i
		self.j = j

class intrfcIDPair:
	"""intrfcIDPair: holds two interface IDs (prevID, nextID)"""
	def __init__(self, prevID: int, nextID: int):
		self.prevID = prevID
		self.nextID = nextID

class iQWrapper:
	"""iQWrapper: holds arrival time and a NetworkMsg reference"""
	def __init__(self, arrival: float, nm: 'NetworkMsg'): # TODO: clarify this as a NetworkMsg
		self.arrival = arrival
		self.nm = nm

class intrfcQStruct:
	"""intrfcQStruct holds information about packets and flows at an interface"""
	def __init__(self, intrfc: Optional['intrfcStruct'], ingress: bool): # TODO: clarify intrfcStruct
		self.intrfc = intrfc  # interface this is attached to
		self.ingress = ingress  # true if this queue is for the ingress side
		self.streaming = False  # true if there are strm packets at this interface
		self.lambda_ = 0.0  # sum of rates of flows approaching this side
		self.msgQueue: List[iQWrapper] = []  # messages enqueued for passage
		self.strmQ: Optional['strmSet'] = None  # stream of background packets # TODO: clarify strmSet

	def qlen(self) -> int:
		return len(self.msgQueue)

	def firstMsg(self) -> Optional['NetworkMsg']:
		if len(self.msgQueue) > 0:
			return self.msgQueue[0].nm
		return None

	def initIntrfcQueueStrm(self, strmRate: float, strmPcktLen: int,
							intrfcBndwdth: float, rng: Any): # TODO: clarify that rng is a rngstream.RngStream
		# Placeholder for strmSet creation
		self.strmQ = createStrmSet(self, strmRate, intrfcBndwdth, strmPcktLen, rng)

	def popNetworkMsg(self) -> Optional['NetworkMsg']: # TODO: clarify NetworkMsg
		if  len(self.msgQueue) == 0:
			return None
		
		iqw = self.msgQueue.pop(0)
		return iqw.nm

	def firstNetworkMsg(self) -> Optional['NetworkMsg']: # TODO: clarify NetworkMsg
		if len(self.msgQueue) == 0:
			return None
		
		return self.msgQueue[0].nm

	# addNetworkMsg adds a network message to the indicated side of the interface
	def addNetworkMsg(self, evtMgr: evtm.EventManager, nm: 'NetworkMsg'): # TODO: clarify NetworkMsg and evtMgr as EventManager
		global msrArrivals
		time = evtMgr.current_seconds()
		nm.intrfcArr = time
		
		intrfc = self.intrfc
		iqw = iQWrapper(arrival=time, nm=nm)
		self.msgQueue.append(iqw)

		inTransit = (self.ingress and self.intrfc.State.IngressTransit) or (not self.ingress and self.intrfc.State.EgressTransit)
		if not self.streaming and not inTransit:
			if len(self.msgQueue) == 1:
				enterIntrfcService(evtMgr, self, nm.MsgID)
			return
	
		if len(self.msgQueue) == 1 and not inTransit:
			# strmQ needs to have been running because this message joined an empty queue
			# advance the flow simulation up to time

			nxtMsg = self.msgQueue[0].nm
			serviceTime = computeServiceTime(nxtMsg.MsgLen, intrfc.State.Bndwdth)
			advanced, qDelay = self.strmQ.queueingDelay(evtMgr.current_seconds(), nxtMsg.intrfcArr,
													  serviceTime, nxtMsg.prevIntrfcID)
			
			msrArrivals = False
			
			# schedule an entry into service at time
			if advanced:
				evtMgr.schedule(self, nxtMsg.MsgID, enterIntrfcService, vrtime.seconds_to_time(qDelay))
			else:
				# we go straight into service and so can skip the scheduling step
				enterIntrfcService(evtMgr, self, nxtMsg.MsgID)

	def addFlowRate(self, flowID: int, rate: float):
		self.lambda_ += rate

	def rmFlowRate(self, flowID: int, rate: float):
		self.lambda_ -= rate

	def Str(self) -> str:
		rtnVec = [str(len(self.msgQueue)), f"{self.lambda_:.12g}"]
		return " % ".join(rtnVec)

def queueStr(msgQueue: List[iQWrapper]) -> str:
	rtn = []
	for iqw in msgQueue:
		element = f"{iqw.arrival} {iqw.nm.MsgID}"
		rtn.append(element)
	return ",".join(rtn)

def computeServiceTime(msgLen: int, bndwdth: float) -> float:
	msgLenMbits = float(8 * msgLen) / 1e6
	return msgLenMbits / bndwdth

# NetworkMsgType give enumeration for message types that may be given to the network
# to carry.  packet is a discrete packet, handled differently from flows.
# srtFlow tags a message that introduces a new flow, endFlow tags one that terminates it,
# and chgFlow tags a message that alters the flow rate on a given flow.
class NetworkMsgType:
	PacketType = 0
	FlowType = 1

class FlowSrcType:
	FlowSrcConst = 0
	FlowSrcRandom = 1

msrArrivals: bool = False

# FlowAction describes the reason for the flow message, that it is starting, ending, or changing the request rate
class FlowAction:
	None_ = 0
	Srt = 1
	Chg = 2
	End = 3

# nmtToStr is a translation table for creating strings from more complex
# data types
nmtToStr: Dict[NetworkMsgType, str] = {NetworkMsgType.PacketType: "packet",
							NetworkMsgType.FlowType: "flow"}

# routeStepIntrfcs maps a pair of device IDs to a pair of interface IDs
# that connect them
routeStepIntrfcs: Dict[intPair, intPair] = {}

# getRouteStepIntrfcs looks up the identity of the interfaces involved in connecting
# the named source and the named destination.  These were previously built into a table
def getRouteStepIntrfcs(srcID: int, dstID: int) -> Tuple[int, int]:
	ip = intPair(srcID, dstID)
	intrfcs = routeStepIntrfcs.get(ip)
	if intrfcs is None:
		inv = routeStepIntrfcs.get(intPair(dstID, srcID))
		if inv is None:
			raise Exception(f"no step between {TopoDevByID[srcID].DevName()} and {TopoDevByID[dstID].DevName()}")
		return inv.nextID, inv.prevID
	return intrfcs.i, intrfcs.j

class NetworkPortal:
	"""NetworkPortal implements the pces interface used to pass traffic between the application layer and the network sim"""
	def __init__(self):
		self.QkNetSim = False
		self.ReturnTo: Dict[int, Any] = {} 			# indexed by connectID
		self.LossRtn: Dict[int, Any] = {}			# indexed by connectID
		self.ReportRtnSrc: Dict[int, Any] = {}		# indexed by connectID
		self.ReportRtnDst: Dict[int, Any] = {}		# indexed by connectID
		self.RequestRate: Dict[int, float] = {}		# indexed by flowID to get requested arrival rate
		self.AcceptedRate: Dict[int, float] = {}	# indexed by flowID to get accepted arrival rate
		self.Mode: Dict[int, str] = {}				# indexed by flowID to record mode of flow
		self.Elastic: Dict[int, bool] = {}			# indexed by flowID to record whether flow is elastic
		self.Pckt: Dict[int, bool] = {}				# indexed by flowID to record whether flow is a packet
		self.Connections: Dict[int, int] = {}		# indexed by connectID to get flowID
		self.InvConnection: Dict[int, int] = {}		# indexed by flowID to get connectID
		self.LatencyConsts: Dict[int, float] = {}	# indexed by flowID to get latency constants on flow's route

	# SetQkNetSim saves the argument as indicating whether latencies
	# should be computed as 'Placed', meaning constant, given the state of the network at the time of computation

	def set_qk_net_sim(self, quick: bool):
		self.QkNetSim = quick

	# ClearRmFlow removes entries from maps indexed
	# by flowID and associated connectID, to help clean up space

	def clear_rm_flow(self, flowID: int):
		connectID = self.InvConnection[flowID]
		self.ReturnTo.pop(connectID, None)
		self.LossRtn.pop(connectID, None)
		self.ReportRtnSrc.pop(connectID, None)
		self.ReportRtnDst.pop(connectID, None)
		self.Connections.pop(connectID, None)

		self.RequestRate.pop(flowID, None)
		self.AcceptedRate.pop(flowID, None)
		self.LatencyConsts.pop(flowID, None)

	# EndptDevModel helps NetworkPortal implement the pces NetworkPortal interface,
	# returning the CPU model associated with a named endpt. Present because the
	# application layer does not otherwise have visibility into the network topology

	def endpt_dev_model(self, devName: str, accelName: str) -> str:
		endpt = EndptDevByName.get(devName) # TODO: clarify EndptDevByName (mrnes.py)
		if endpt is None:
			return ""
		
		if not accelName:
			return endpt.EndptModel
		
		accelModel = endpt.EndptAccelModel.get(accelName)
		if accelModel is None:
			return ""
		
		return accelModel

	# Depart is called to return an application message being carried through
	# the network back to the application layer

	def depart(self, evtMgr: evtm.EventManager, devName: str, nm: NetworkMsg):
		connectID = nm.ConnectID

		# may not require knowledge that delivery made it
		rtnRec = self.ReturnTo.get(connectID)
		if rtnRec is None or getattr(rtnRec, 'rtnCxt', None) is None:
			return
		
		rtnRec.prArrvl *= nm.PrArrvl
		rtnRec.pckts -= 1

		# if rtnRec.pckts is not zero there are more packets coming associated
		# with connectID and so we exit
		if rtnRec.pckts > 0:
			return
		
		prArrvl = rtnRec.prArrvl

		# so we can return now
		rtnMsg = RtnMsgStruct()
		rtnMsg.Latency = evtMgr.current_seconds() - nm.StartTime
		
		if nm.carries_pckt():
			rtnMsg.Rate = nm.PcktRate
			rtnMsg.PrLoss = (1.0 - prArrvl)
		else:
			rtnMsg.Rate = self.AcceptedRate[nm.FlowID]
		
		rtnMsg.Msg = nm.Msg

		rtnMsg.DevIDs = [rtStep.devID for rtStep in nm.Route]

		rtnCxt = rtnRec.rtnCxt
		rtnFunc = rtnRec.rtnFunc

		endpt = EndptDevByName[devName]

		# schedule the re-integration into the application simulator, delaying by
		# the interrupt handler cost (if any)
		evtMgr.Schedule(rtnCxt, rtnMsg, rtnFunc, vrtime.seconds_to_time(endpt.EndptState.InterruptDelay))
		
		# clean up the records for this connectID
		self.ReturnTo.pop(connectID, None)
		self.LossRtn.pop(connectID, None)
		self.ReportRtnSrc.pop(connectID, None)
		self.ReportRtnDst.pop(connectID, None)

	# requestedLoadFracVec computes the relative requested rate for a flow
	# among a list of flows. Used to rescale accepted rates
	def requested_load_frac_vec(self, vec: List[int]) -> List[float]:
		rtn = [0.0] * len(vec)
		agg = 0.0
		
		# gather up the rates in an array and compute the normalizing sum
		for idx, flowID in enumerate(vec):
			rate = self.RequestRate[flowID]
			agg += rate
			rtn[idx] = rate

		# normalize
		for idx in range(len(vec)):
			rtn[idx] /= agg
		return rtn
	
	# Arrive is called at the point an application message is received by the network
	# and a new connectID is created (and returned) to track it. It saves information needed to re-integrate
	# the application message into the application layer when the message arrives at its destination

	def arrive(self, rtns: RtnDescs, frames: int) -> int:
		# record how to transition from network to upper layer, through event handler at upper layer
		rtnRec = rtnRecord(rtnCxt=rtns.Rtn.Cxt, rtnFunc=rtns.Rtn.EvtHdlr, prArrvl=1.0, pckts=frames) # TODO: clarify rtnRecord (transition.py)
		
		connectID = nxtConnectID()
		self.ReturnTo[connectID] = rtnRec

		# if requested, record how to notify source end of connection at upper layer through connection
		if getattr(rtns, 'Src', None) is not None:
			rtnRec = rtnRecord(rtnCxt=rtns.Src.Cxt, rtnFunc=rtns.Src.EvtHdlr, prArrvl=1.0, pckts=1)
			self.ReportRtnSrc[connectID] = rtnRec

		# if requested, record how to notify destination end of connection at upper layer through event handler
		if getattr(rtns, 'Dst', None) is not None:
			rtnRec = rtnRecord(rtnCxt=rtns.Dst.Cxt, rtnFunc=rtns.Dst.EvtHdlr, prArrvl=1.0, pckts=1)
			self.ReportRtnDst[connectID] = rtnRec

		# if requested, record how to notify occurance of loss at upper layer, through event handler
		if getattr(rtns, 'Loss', None) is not None:
			rtnRec = rtnRecord(rtnCxt=rtns.Loss.Cxt, rtnFunc=rtns.Loss.EvtHdlr, prArrvl=1.0, pckts=1)
			self.LossRtn[connectID] = rtnRec
		
		return connectID
	
	###################### BEGIN TRANSITION.PY METHODS #########################

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
	# | Message		 | connType	  | flowAction	| connLatency		   | flowID
	# | --------------- | ------------- | ------------- | --------------------- | ----------------------- |
	# | Discrete Packet | DiscreteConn  | N/A		   | Zero, Place, Simulate | >0 => embedded		  |
	# | Flow			| FlowConn	  | Srt, Chg, End | Zero, Place, Simulate | flowID>0				|
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
		is_pckt = (conn_desc.Type == transition.ConnType.DiscreteConn)
		
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
		if conn_desc.Type == transition.ConnType.DiscreteConn:
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
		if conn_desc.Type == transition.ConnType.FlowConn:
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
			self.LatencyConsts[flow_id] = transition.build_flow(flow_id, route)
		
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
		rfs = transition.RprtRate()
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
				#	For an elastic flow we may need to squeeze
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
		
		if conn_latency == transition.ConnLatency.Zero:
			# the message's position in the route list---the last step
			nm.StepIdx = len(route) - 1
			self.send_immediate(evt_mgr, nm)
		elif conn_latency == transition.ConnLatency.Place:
			# the message's position in the route list---the last step
			nm.StepIdx = len(route) - 1
			self.place_net_msg(evt_mgr, nm, offset)
		elif conn_latency == transition.ConnLatency.Simulate:
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
		if latency_type == transition.ConnLatency.Zero:
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
	###################### END TRANSITION.PY METHODS #########################

# ActivePortal remembers the most recent NetworkPortal created
# (there should be only one call to CreateNetworkPortal...)
ActivePortal: NetworkPortal = None

class ActiveRec:
	"""ActiveRec holds number and rate for a flow."""
	def __init__(self, Number: int = 0, Rate: float = 0.0):
		self.Number = Number
		self.Rate = Rate

# CreateNetworkPortal is a constructor, passed a flag indicating which
# of two network simulation modes to use, passes a flag indicating whether
# packets should be passed whole, and writes the NetworkPortal pointer into a global variable

def CreateNetworkPortal() -> NetworkPortal:
	global ActivePortal, connections
	# return existing portal if there is one
	if ActivePortal is not None:
		return ActivePortal
	
	# otherwise create a new one (this prevents multiple portals)
	np = NetworkPortal()
	# set default settings
	np.QkNetSim = False
	np.ReturnTo = {}
	np.LossRtn = {}
	np.ReportRtnSrc = {}
	np.ReportRtnDst = {}
	np.RequestRate = {}
	np.AcceptedRate = {}
	np.Elastic = {}
	np.Pckt = {}
	np.Mode = {}
	np.Connections = {}
	np.InvConnection = {}
	np.LatencyConsts = {}

	# save the mrnes memory space version
	ActivePortal = np
	connections = 0
	return np

# RtnMsgStruct formats the report passed from the network to the application calling it
class RtnMsgStruct:
	"""RtnMsgStruct: report from network to application"""
	def __init__(self):
		self.Latency: float = 0.0 		# span of time (secs) from srcDev to dstDev
		self.Rate: float = 0.0   		# for a flow, its accept rate.  For a packet, the minimum non-flow bandwidth at a
			# network or interface it encountered
		self.PrLoss: float = 0.0 		# estimated probability of having been dropped somewhere in transit
		self.DevIDs: List[int] = [] 	# list of ids of devices visited on transition of network
		self.Msg: Any = None 			# msg introduced at EnterNetwork

# DevCode is the base type for an enumerated type of network devices
class DevCode:
	EndptCode = 0
	SwitchCode = 1
	RouterCode = 2
	UnknownCode = 3

# DevCodeFromStr returns the devCode corresponding to a string name for it
def DevCodeFromStr(code: str) -> DevCode:
	if code in ["Endpt", "Endpoint", "endpt"]:
		return DevCode.EndptCode
	elif code in ["Switch", "switch"]:
		return DevCode.SwitchCode
	elif code in ["Router", "router", "rtr"]:
		return DevCode.RouterCode
	else:
		return DevCode.UnknownCode

# dev_code_to_str returns a string corresponding to an input devCode for it
def dev_code_to_str(code: DevCode) -> str:
	if code == DevCode.EndptCode:
		return "Endpt"
	elif code == DevCode.SwitchCode:
		return "Switch"
	elif code == DevCode.RouterCode:
		return "Router"
	elif code == DevCode.UnknownCode:
		return "Unknown"
	
	return "Unknown"

# NetworkScale is the base type for an enumerated type of network type descriptions
class NetworkScale:
	LAN = 0
	WAN = 1
	T3 = 2
	T2 = 3
	T1 = 4
	GeneralNet = 5

# NetScaleFromStr returns the networkScale corresponding to a string name for it
def NetScaleFromStr(netScale: str) -> NetworkScale:
	if netScale == "LAN":
		return NetworkScale.LAN
	elif netScale == "WAN":
		return NetworkScale.WAN
	elif netScale == "T3":
		return NetworkScale.T3
	elif netScale == "T2":
		return NetworkScale.T2
	elif netScale == "T1":
		return NetworkScale.T1
	else:
		return NetworkScale.GeneralNet

# NetScaleToStr returns a string name that corresponds to an input networkScale
def NetScaleToStr(ntype: NetworkScale) -> str:
	if ntype == NetworkScale.LAN:
		return "LAN"
	elif ntype == NetworkScale.WAN:
		return "WAN"
	elif ntype == NetworkScale.T3:
		return "T3"
	elif ntype == NetworkScale.T2:
		return "T2"
	elif ntype == NetworkScale.T1:
		return "T1"
	elif ntype == NetworkScale.GeneralNet:
		return "GeneralNet"
	else:
		return "GeneralNet"

# NetworkMedia is the base type for an enumerated type of comm network media
class NetworkMedia:
	Wired = 0  # Wired NetworkMedia = iota
	Wireless = 1
	UnknownMedia = 2

# net_media_from_str returns the networkMedia type corresponding to the input string name
def net_media_from_str(media: str) -> NetworkMedia:
	if media in ("Wired", "wired"):
		return NetworkMedia.Wired
	elif media in ("wireless", "Wireless"):
		return NetworkMedia.Wireless
	else:
		return NetworkMedia.UnknownMedia

# every new network connection is given a unique connectID upon arrival
connections = 0

def nxtConnectID() -> int:
	global connections
	connections += 1
	return connections

# DFS: index by FlowID, yields map of ingress intrfc ID to egress intrfc ID
# (DFS = Dict[int, intrfcIDPair])
DFS = Dict[int, 'intrfcIDPair'] # TODO: make sure this is correct

# TopoDev interface specifies the functionality different device types provide
class TopoDev(ABC):
	"""TopoDev interface: functionality for network device types."""
	@abstractmethod
	def dev_name(self) -> str:
		"""Every device has a unique name."""
		pass

	@abstractmethod
	def dev_id(self) -> int:
		"""Every device has a unique integer id."""
		pass

	@abstractmethod
	def dev_type(self) -> DevCode:
		"""Every device is one of the devCode types"""
		pass

	@abstractmethod
	def dev_model(self) -> str:
		"""Model (or CPU) of device."""
		pass

	@abstractmethod
	def dev_intrfcs(self) -> intrfcStruct:
		"""We can get from devices a list of the interfaces they endpoint, if any."""
		pass

	@abstractmethod
	def dev_delay(self, msg: NetworkMsg) -> float:
		"""Every device can be queried for the delay it introduces for an operation."""
		pass

	@abstractmethod
	def dev_state(self) -> any:
		"""Every device has a structure of state that can be accessed."""
		pass

	@abstractmethod
	def dev_rng(self) -> rngStream.RngStream:
		"""Every device has its own RNG stream."""
		pass

	@abstractmethod
	def dev_add_active(self, msg: NetworkMsg):
		"""Add a connection ID to the device's list of active connections."""
		pass

	@abstractmethod
	def dev_rm_active(self, connect_id: int):
		"""Remove a connection ID from the device's list of active connections."""
		pass

	@abstractmethod
	def dev_forward(self) -> DFS:
		"""Return a mapping of ingress interface IDs to egress interface IDs indexed by FlowID."""
		pass

	@abstractmethod
	def log_net_event(self, time: vrtime.Time, msg: NetworkMsg, event: str):
		"""Log a network event for the device."""
		pass

# paramObj interface is satisfied by every network object that
# can be configured at run-time with performance parameters.
# These are intrfcStruct, networkStruct, switchDev, endptDev, routerDev
class paramObj(ABC):
	"""paramObj interface: for network objects configurable at run-time with performance parameters."""
	@abstractmethod
	def match_param(self, name: str, name2: str) -> bool:
		pass
	
	@abstractmethod
	def set_param(self, name: str, value: valueStruct):
		pass

	@abstractmethod
	def param_obj_name(self, name: str) -> str:
		pass

	@abstractmethod
	def log_net_event(self, time: vrtime.Time, msg: NetworkMsg, event: str):
		"""Log a network event for the device."""
		pass

# The intrfcStruct holds information about a network interface embedded in a device
class intrfcStruct:
	def __init__(self):
		self.Name: 		str = ""  					# unique name, probably generated automatically
		self.Groups: 	list[str] = []  			# list of groups this interface may belong to
		self.Number: 	int = 0  					# unique integer id, probably generated automatically
		self.DevType: 	DevCode = None  			# device code of the device holding the interface
		self.Media: 	NetworkMedia = None 		# media of the network the interface interacts with
		self.Device: 	TopoDev = None  			# pointer to the device holding the interface
		self.PrmDev: 	paramObj = None  			# pointer to the device holding the interface as a paramObj
		self.Cable: 	intrfcStruct = None 		# For a wired interface, points to the "other" interface in the connection
		self.Carry: 	list[intrfcStruct] = []  	# points to the "other" interface in a connection
		self.FlowModel: str = ""  					# code for method for introducing delay due to flows
		self.Wireless: 	list[intrfcStruct] = []  	# For a wired interface, points to the "other" interface in the connection
		self.Faces: 	networkStruct = []  		# pointer to the network the interface interacts with
		self.State: 	intrfcState = None  		# pointer to the interface's block of state information
	
	# addTrace gathers information about an interface and message passing though it, and prints it out
	def add_trace(self, label: str, nm: NetworkMsg, t: float):
		
		# return if we aren't asking for a trace on this interface
		if not self.State.Trace:
			return
		
		si = ShortIntrfc()
		si.DevName = self.Device.DevName()
		si.Faces = self.Faces.Name
		flwID = nm.FlowID
		si.FlowID = flwID
		si.ToIngress = self.State.ToIngress[flwID]
		si.ThruIngress = self.State.ThruIngress[flwID]
		si.ToEgress = self.State.ToEgress[flwID]
		si.ThruEgress = self.State.ThruEgress[flwID]
		si.NetMsgID = nm.MsgID
		si.NetMsgType = nm.NetMsgType
		si.PrArrvl = nm.PrArrvl
		si.Time = t
		siStr = si.Serialize()
		siStr = siStr.replace("\n", " ", -1)
		siStr += "\n"
		print(label, siStr)
	

	def match_param(self, attrb_name: str, attrb_value: str) -> bool:
		"""
		match_param is used to determine whether a run-time parameter description
		should be applied to the interface. Its definition here helps selfStruct satisfy
		paramObj interface. To apply or not to apply depends in part on whether the
		attribute given match_param as input matches what the interface has. The
		interface attributes that can be tested are the device type of device that endpts it, and the
		media type of the network it interacts with
		"""
		switch = {
			"name": lambda: self.Name == attrb_value,
			"group": lambda: attrb_value in self.Groups,
			"media": lambda: net_media_from_str(attrb_value) == self.Media,
			"devtype": lambda: dev_code_to_str(self.Device.DevType()) == attrb_value,
			"devname": lambda: self.Device.DevName() == attrb_value,
			"faces": lambda: self.Faces.Name == attrb_value,
		}
		return switch.get(attrb_name, lambda: False)()

	# set_param assigns the parameter named in input with the value given in the input.
	# N.B. the valueStruct has fields for integer, float, and string values.  Pick the appropriate one.
	# set_param's definition here helps intrfcStruct satisfy the paramObj interface.
	def set_param(self, param_type: str, value: valueStruct):
		# latency, delay, and bandwidth are floats
		if param_type == "latency":
			# parameters units of latency are already in seconds
			self.State.Latency = value.floatValue
		elif param_type == "delay":
			# parameters units of delay are already in seconds
			self.State.Delay = value.floatValue
		elif param_type == "bandwidth":
			# units of bandwidth are Mbits/sec
			self.State.Bndwdth = value.floatValue
			self.State.IngressIntrfcQ.strmQ.setBndwdth(value.floatValue)
			self.State.EgressIntrfcQ.strmQ.setBndwdth(value.floatValue)
		elif param_type == "buffer":
			# units of buffer size are in Mbytes
			self.State.BufferSize = value.floatValue
		elif param_type == "MTU":
			# number of bytes in maximally sized packet
			self.State.MTU = value.intValue
		elif param_type == "trace":
			self.State.Trace = (value.intValue == 1)
		elif param_type == "drop":
			self.State.Drop = value.boolValue

	# log_net_event creates and logs a network event from a message passing
	# through this interface
	def log_net_event(self, time: vrtime.Time, nm: NetworkMsg, op: str):
		if not self.State.Trace:
			return
		
		trace.add_net_trace(devTraceMgr, time, nm, self.Number, op) # TODO: clarify devTraceMgr (mrnes.py)

	# param_obj_name helps intrfcStruct satisfy paramObj interface, returns interface name
	def param_obj_name(self) -> str:
		return self.Name

	# pckt_drop returns a flag indicating whether we're simulating packet drops
	def pckt_drop(self) -> bool:
		return self.State.Drop

	# add_flow initializes the To and Thru maps for the interface
	def add_flow(self, flow_id: int, ingress: bool):
		if ingress:
			self.State.ToIngress[flow_id] = 0.0
			self.State.ThruIngress[flow_id] = 0.0
		else:
			self.State.ToEgress[flow_id] = 0.0
			self.State.ThruEgress[flow_id] = 0.0

	# is_congested determines whether the interface is congested,
	# meaning that the bandwidth used by elastic flows is greater than or
	# equal to the unreserved bandwidth
	def is_congested(self, ingress: bool) -> bool:
		used_bndwdth = 0.0

		if ingress:
			for rate in self.State.ToIngress.values():
				used_bndwdth += rate
		else:
			for rate in self.State.ToEgress.values():
				used_bndwdth += rate

		bndwdth = self.State.Bndwdth
		return abs(used_bndwdth - bndwdth) < 1e-3

	# chg_flow_rate is called in the midst of changing the flow rates.
	# The rate value 'rate' is one that flow has at this point in the
	# computation, and the per-flow interface data structures are adjusted
	# to reflect that.
	# src_intrfc_id identifies the interface from which the flow came to intrfc,
	# flow_id the flow with the rate change
	def chg_flow_rate(self, src_intrfc_id: int, flow_id: int, rate: float, ingress: bool):
		ifq: intrfcQStruct = None
		old_rate = 0.0

		if ingress:
			ifq = self.State.IngressIntrfcQ
			old_rate = self.State.ToIngress.get(flow_id, 0.0)
			self.State.ToIngress[flow_id] = rate
			self.State.ThruIngress[flow_id] = rate
			self.State.IngressLambda += (rate - old_rate)
		else:
			ifq = self.State.EgressIntrfcQ
			old_rate = self.State.ToEgress.get(flow_id, 0.0)
			self.State.ToEgress[flow_id] = rate
			self.State.ThruEgress[flow_id] = rate
			self.State.EgressLambda += (rate - old_rate)
		
		if ifq:
			ifq.lambda_ += (rate - old_rate)
			ifq.strmQ.adjust_strm_rate(src_intrfc_id, flow_id, ifq.lambda_)

	# rm_flow adjusts data structures to reflect the removal of the identified flow
	# formerly having the identified rate
	def rm_flow(self, flow_id: int, rate: float, ingress: bool):
		if ingress:
			self.State.ToIngress.pop(flow_id, None)
			self.State.ThruIngress.pop(flow_id, None)

			# need have the removed flow's prior rate
			# old_rate = self.State.IngressLambda
			# self.State.IngressLambda = old_rate - rate
		else:
			self.State.ToEgress.pop(flow_id, None)
			self.State.ThruEgress.pop(flow_id, None)

			# need have the removed flow's prior rate
			# old_rate = self.State.EgressLambda
			# self.State.EgressLambda = old_rate - rate



# The intrfcState holds parameters descriptive of the interface's capabilities
class intrfcState:
	def __init__(self, intrfc: intrfcStruct):
		self.Bndwdth: 		float = 0.0  		# maximum bandwidth (in Mbytes/sec)
		self.BufferSize: 	float = 0.0  		# buffer capacity (in Mbytes)
		self.Latency: 		float = 0.0  		# time the leading bit takes to traverse the wire out of the interface
		self.Delay: 		float = 0.0  		# time the leading bit takes to traverse the interface

		self.IngressTransit: 		bool = False
		self.IngressTransitMsgID: 	int = 0
		self.EgressTransit: 		bool = False
		self.EgressTransitMsgID: 	int = 0

		self.MTU: 	int = 0  		# maximum packet size (bytes)
		self.Trace: bool = False  	# switch for calling add trace
		self.Drop: 	bool = False  	# whether to permit packet drops

		self.ToIngress: 	Dict[int, float] = {}  # sum of flow rates into ingress side of device
		self.ThruIngress: 	Dict[int, float] = {}  # sum of flow rates out of ingress side of device
		self.ToEgress: 		Dict[int, float] = {}  # sum of flow rates into egress side of device
		self.ThruEgress: 	Dict[int, float] = {}  # sum of flow rates out of egress side device

		self.IngressLambda: 	float = 0.0  			# sum of rates of flows approach interface from ingress side.
		self.EgressLambda: 		float = 0.0
		self.FlowModel: 		str = ""
		self.IngressIntrfcQ:	intrfcQStruct = None
		self.EgressIntrfcQ:		intrfcQStruct = None
		self.StrmServiceTime: 	float = 0.0

# createIntrfcState is a constructor, assumes defaults on unspecified attributes
def create_intrfc_state(intrfc: intrfcStruct) -> intrfcState:
	iss = intrfcState()
	iss.Delay = 1e+6  # in seconds!  Set this way so that if not initialized we'll notice
	iss.Latency = 1e+6
	iss.MTU = 1560  # in bytes Set for Ethernet2 MTU, should change if wireless
	iss.ToIngress = {}
	iss.ThruIngress = {}
	iss.ToEgress = {}
	iss.ThruEgress = {}
	iss.EgressLambda = 0.0
	iss.IngressLambda = 0.0
	iss.FlowModel = "expon"
	iss.IngressIntrfcQ = create_intrfc_queue(intrfc, True)

	rng = intrfc.Device.DevRng()
	
	# put in default stream rate of zero and bandwidth of 1000, to be modified as updated.
	iss.IngressIntrfcQ.init_intrfc_queue_strm(0.0, 1000, 1000.0, rng)
	iss.EgressIntrfcQ = create_intrfc_queue(intrfc, False)
	iss.EgressIntrfcQ.init_intrfc_queue_strm(0.0, 1000, 1000.0, rng)
	return iss

# createIntrfcStruct is a constructor, building an intrfcStruct from a desc description of the interface
def create_intrfc_struct(intrfc: IntrfcDesc) -> intrfcStruct:
	is_ = intrfcStruct()
	is_.Groups = intrfc.Groups

	# name comes from desc description
	is_.Name = intrfc.Name
	
	# unique id is locally generated
	is_.Number = nxtID()
	
	# desc representation codes the device type as a string
	if intrfc.DevType == "Endpt":
		is_.DevType = DevCode.EndptCode
	elif intrfc.DevType == "Router":
		is_.DevType = DevCode.RouterCode
	elif intrfc.DevType == "Switch":
		is_.DevType = DevCode.SwitchCode

	
	# The desc description gives the name of the device endpting the interface.
	# We can use this to look up the locally constructed representation of the device and save a pointer to it
	is_.Device = TopoDevByName[intrfc.Device]
	is_.PrmDev = paramObjByName[intrfc.Device]

	# desc representation codes the media type as a string
	if intrfc.MediaType in ("wired", "Wired"):
		is_.Media = NetworkMedia.Wired
	elif intrfc.MediaType in ("wireless", "Wireless"):
		is_.Media = NetworkMedia.Wireless
	else:
		is_.Media = NetworkMedia.UnknownMedia
	
	is_.Wireless = []
	is_.Carry = []
	is_.State = create_intrfc_state(is_)
	return is_

# non-preemptive priority
# k=1 is highest priority
# W_k : mean waiting time of class-k msgs
# S_k : mean service time of class-k msg
# lambda_k : arrival rate class k
# rho_k : load of class-k, rho_k = lambda_k*S_k
# R : mean residual of server on arrival : (server util)*D/2
#
#  W_k = R/((1-rho_{1}-rho_{2}- ... -rho_{k-1})*(1-rho_1-rho_2- ... -rho_k))
#
#  Mean time in system of class-k msg is T_k = W_k+S_k
#
# for our purposes we will use k=0 for least class, and use the formula
#  W_0 = R/((1-rho_{1}-rho_{2}- ... -rho_{k-1})*(1-rho_1-rho_2- ... -rho_{k-1}-rho_0))

# ShortIntrfc stores information we serialize for storage in a trace
class ShortIntrfc:
	def __init__(self):
		self.DevName: 		str = ""
		self.Faces: 		str = ""
		self.ToIngress: 	float = 0.0
		self.ThruIngress: 	float = 0.0
		self.ToEgress: 		float = 0.0
		self.ThruEgress: 	float = 0.0
		self.FlowID: 		int = 0
		self.NetMsgType: 	NetworkMsgType = 0
		self.NetMsgID: 		int = 0
		self.Rate: 			float = 0.0
		self.PrArrvl: 		float = 0.0
		self.Time: 			float = 0.0

	# Serialize turns a ShortIntrfc into a string, in yaml format
	def Serialize(self) -> str:
		import yaml
		return yaml.dump(self.__dict__)

# TODO: What is this function doing? Why is is not used?

# link_intrfc_struct sets the 'connect' and 'faces' values
# of an intrfcStruct based on the names coded in a IntrfcDesc.
def link_intrfc_struct(self, intrfc_desc):
	# look up the intrfcStruct corresponding to the interface named in input intrfc
	is_ = IntrfcByName[intrfc_desc.Name]

	# in IntrfcDesc the 'Cable' field is a string, holding the name of the target interface
	if len(intrfc_desc.Cable) > 0:
		if intrfc_desc.Cable not in IntrfcByName:
			raise Exception("intrfc cable connection goof")
		
		is_.Cable = IntrfcByName[intrfc_desc.Cable]

	# in IntrfcDesc the 'Carry' field is a list of strings, holding the names of target interfaces
	if len(intrfc_desc.Carry) > 0:
		for cintrfc_name in intrfc_desc.Carry:
			if cintrfc_name not in IntrfcByName:
				raise Exception("intrfc cable connection goof")
			
			is_.Carry.append(IntrfcByName[cintrfc_name])

	if len(intrfc_desc.Wireless) > 0:
		for intrfc_name in intrfc_desc.Wireless:
			is_.Wireless.append(IntrfcByName[intrfc_name])

	# in IntrfcDesc the 'Faces' field is a string, holding the name of the network the interface interacts with
	if len(intrfc_desc.Faces) > 0:
		is_.Faces = NetworkByName[intrfc_desc.Faces]


class networkStruct:
	"""
	A networkStruct holds the attributes of one of the model's communication subnetworks
	"""
	def __init__(self):
		self.Name: 			str = ""				# unique name
		self.Groups: 		list[str] = []	  	# list of groups to which network belongs
		self.Number: 		int = 0	   			# unique integer id
		self.NetScale: 		NetworkScale = None  	# type, e.g., LAN, WAN, etc.
		self.NetMedia: 		NetworkMedia = None  	# communication fabric, e.g., wired, wireless
		self.NetRouters: 	list[routerDev] = []  	# list of pointers to routerDevs with interfaces that face this subnetwork
		self.NetSwitches: 	list[switchDev] = [] 	# list of pointers to switchDevs with interfaces that face this subnetwork
		self.NetEndpts: 	list[endptDev] = []   	# list of pointers to endptDevs with interfaces that face this subnetwork
		self.NetState: 		networkState = None  	# pointer to a block of information comprising the network 'state'

	# AddFlow updates a networkStruct's data structures to add
	# a major flow
	def add_flow(self, flow_id: int, ifcpr):
		if flow_id not in self.NetState.Flows:
			self.NetState.Flows[flow_id] = ActiveRec(Number=0, Rate=0.0)
		ar = self.NetState.Flows[flow_id]
		ar.Number += 1
		self.NetState.Flows[flow_id] = ar
		self.NetState.Forward[flow_id] = {}
		self.NetState.Forward[flow_id][ifcpr] = 0.0

	# RmFlow updates a networkStruct's data structures to reflect
	# removal of a major flow
	def rm_flow(self, flow_id: int, ifcpr):
		rate = self.NetState.Forward[flow_id][ifcpr]
		del self.NetState.Forward[flow_id][ifcpr]
		ar = self.NetState.Flows[flow_id]
		ar.Number -= 1
		ar.Rate -= rate
		if ar.Number == 0:
			del self.NetState.Flows[flow_id]
			del self.NetState.Forward[flow_id]
		else:
			self.NetState.Flows[flow_id] = ar

	# ChgFlowRate updates a networkStruct's data structures to reflect
	# a change in the requested flow rate for the named flow
	def chg_flow_rate(self, flow_id: int, ifcpr, rate: float):
		# initialize (if needed) the forward entry for this flow
		if flow_id not in self.NetState.Forward:
			self.NetState.Forward[flow_id] = {}
		old_rate = self.NetState.Forward[flow_id].get(ifcpr, 0.0)

		# compute the change of rate and add the change to the variables
		# that accumulate the rates
		delta_rate = rate - old_rate

		ar = self.NetState.Flows[flow_id]
		ar.Rate += delta_rate
		self.NetState.Flows[flow_id] = ar

		# save the new rate
		self.NetState.Forward[flow_id][ifcpr] = rate

	# determine whether the network state between the source and destination interfaces is congested,
	# which can only happen if the interface bandwidth is larger than the configured bandwidth
	# between these endpoints, and is busy enough to overwhelm it
	def is_congested(self, src_intrfc, dst_intrfc):
		# gather the fixed bandwdth for the source
		used_bndwdth = min(src_intrfc.State.Bndwdth, dst_intrfc.State.Bndwdth)
		
		seen_flows = set()
		for flw_id, rate in src_intrfc.State.ThruEgress.items():
			used_bndwdth += rate
			seen_flows.add(flw_id)
		
		for flw_id, rate in dst_intrfc.State.ToIngress.items():
			if flw_id in seen_flows:
				continue
			used_bndwdth += rate
		
		net = src_intrfc.Faces
		net_useable_bw = net.NetState.Bndwdth
		if used_bndwdth < net_useable_bw and not (abs(used_bndwdth - net_useable_bw) < 1e-3):
			return False
		return True

	# initNetworkStruct transforms information from the desc description
	# of a network to its networkStruct representation.  This is separated from
	# the createNetworkStruct constructor because it requires that the brdcstDmnByName
	# and RouterDevByName lists have been created, which in turn requires that
	# the router constructors have already been called.  So the call to initNetworkStruct
	# is delayed until all of the network device constructors have been called.
	# TODO: I wonder if we still need this considering that python doesn't have pointers
	def init_network_struct(self, nd):
		# in NetworkDesc a router is referred to through its string name
		self.NetRouters = []
		for rtr_name in nd.Routers:
			# use the router name from the desc representation to find the run-time pointer
			# to the router and append to the network's list
			self.add_router(RouterDevByName[rtr_name])
		
		self.NetEndpts = []
		for endpt_name in nd.Endpts:
			# use the endpoint name from the desc representation to find the run-time pointer
			# to the endpoint and append to the network's list
			self.add_endpt(EndptDevByName[endpt_name])
		
		self.NetSwitches = []
		for switch_name in nd.Switches:
			# use the switch name from the desc representation to find the run-time pointer
			# to the switch and append to the network's list
			self.add_switch(SwitchDevByName[switch_name])
		
		self.Groups = nd.Groups

	# createNetworkStruct is a constructor that initialized some of the features of the networkStruct
	# from their expression in a desc representation of the network
	@staticmethod
	# TODO: is this the right way to do this?
	def create_network_struct(nd):
		ns = networkStruct()
		ns.Name = nd.Name
		ns.Groups = []
		# get a unique integer id locally
		ns.Number = nxtID() # TODO: fron mrnes.py
		# get a netScale type from a desc string expression of it
		ns.NetScale = NetScaleFromStr(nd.NetScale)
		# get a netMedia type from a desc string expression of it
		ns.NetMedia = NetMediaFromStr(nd.MediaType)
		
		# initialize the Router lists, to be filled in by
		# initNetworkStruct after the router constructors are called
		ns.NetRouters = []
		ns.NetEndpts = []

		# make the state structure, will flesh it out from run-time configuration parameters
		ns.NetState = networkStruct.create_network_state(ns.Name)
		return ns

	# createNetworkState constructs the data for a networkState struct
	@staticmethod
	def create_network_state(name: str):
		ns = networkStruct.networkState()
		ns.Flows = {}
		ns.Forward = {}
		ns.ClassBndwdth = {}
		ns.Packets = 0
		ns.Drop = False
		ns.Rngstrm = rngstream.New(name)
		return ns

	# NetLatency estimates the time required by a message to traverse the network,
	# using the mean time in system of an M/D/1 queue
	def net_latency(self, nm):
		# get the rate activitiy on this channel
		lambda_ = self.channel_load(nm)

		# compute the service mean
		srv = (float(nm.MsgLen * 8) / 1e6) / self.NetState.Bndwdth
		
		rho = lambda_ * srv
		in_sys = srv + rho / (2 * (1.0 / srv) * (1 - rho))
		return self.NetState.Latency + in_sys

	# matchParam is used to determine whether a run-time parameter description
	# should be applied to the network. Its definition here helps networkStruct satisfy
	# paramObj interface.  To apply or not to apply depends in part on whether the
	# attribute given matchParam as input matches what the interface has. The
	# interface attributes that can be tested are the media type, and the nework type
	def match_param(self, attrb_name: str, attrb_value: str) -> bool:
		switch = {
			"name": lambda: self.Name == attrb_value,
			"group": lambda: attrb_value in self.Groups,
			"media": lambda: NetMediaFromStr(attrb_value) == self.NetMedia,
			"scale": lambda: self.NetScale == NetScaleFromStr(attrb_value),
		}
		return switch.get(attrb_name, lambda: False)()

	# setParam assigns the parameter named in input with the value given in the input.
	# N.B. the valueStruct has fields for integer, float, and string values.  Pick the appropriate one.
	# setParam's definition here helps networkStruct satisfy the paramObj interface.
	def set_param(self, param_type: str, value):
		# for some attributes we'll want the string-based value, for others the floating point one
		flt_value = value.floatValue

		# branch on the parameter being set
		if param_type == "latency":
			self.NetState.Latency = flt_value
		elif param_type == "bandwidth":
			self.NetState.Bndwdth = flt_value
		elif param_type == "capacity":
			self.NetState.Capacity = flt_value
		elif param_type == "trace":
			self.NetState.Trace = value.boolValue
		elif param_type == "drop":
			self.NetState.Drop = value.boolValue

	# paramObjName helps networkStruct satisfy paramObj interface, returns network name
	def param_obj_name(self) -> str:
		return self.Name

	# LogNetEvent saves a log event associated with the network
	def log_net_event(self, time, nm, op):
		if not self.NetState.Trace:
			return
		AddNetTrace(devTraceMgr, time, nm, self.Number, op)

	# AddRouter includes the router given as input parameter on the network list of routers that face it
	def add_router(self, newrtr):
		# skip if rtr already exists in network NetRouters list
		for rtr in self.NetRouters:
			if rtr == newrtr or getattr(rtr, 'RouterName', None) == getattr(newrtr, 'RouterName', None):
				return
		self.NetRouters.append(newrtr)

	# addEndpt includes the endpt given as input parameter on the network list of endpts that face it
	def add_endpt(self, newendpt):
		# skip if endpt already exists in network NetEndpts list
		for endpt in self.NetEndpts:
			if endpt == newendpt or getattr(endpt, 'EndptName', None) == getattr(newendpt, 'EndptName', None):
				return
		self.NetEndpts.append(newendpt)

	# addSwitch includes the switch given as input parameter on the network list of switches that face it
	def add_switch(self, newswtch):
		# skip if swtch already exists in network NetSwitches list
		for swtch in self.NetSwitches:
			if swtch == newswtch or getattr(swtch, 'SwitchName', None) == getattr(newswtch, 'SwitchName', None):
				return
		self.NetSwitches.append(newswtch)

	# ServiceRate identifies the rate of the network when viewed as a separated server,
	# which means the bndwdth of the channel (possibly implicit) excluding known flows
	def channel_load(self, nm: NetworkMsg) -> float: # TODO: networkMsg defined later
		rt_step = nm.Route[nm.StepIdx]
		src_intrfc = IntrfcByID[rt_step.srcIntrfcID]
		dst_intrfc = IntrfcByID[rt_step.dstIntrfcID]
		return src_intrfc.State.EgressLambda + dst_intrfc.State.IngressLambda

	# PcktDrop returns the packet drop bit for the network
	def pckt_drop(self) -> bool:
		return self.NetState.Drop

# TODO: double check this
class networkState:
	"""
	a networkState holds extra information used by the network
	"""
	def __init__(self):
		self.Rngstrm = None  # pointer to a random number generator
		self.Latency = 0.0
		self.Bndwdth = 0.0
		self.Capacity = 0.0
		self.Trace = False  # switch for calling add trace
		self.Drop = False  # whether to support packet drops at interface
		self.Flows = {}  # map[int]ActiveRec
		self.Forward = {}  # map[int]map[intrfcPair]float64
		self.ClassBndwdth = {}  # map[int]float64
		self.Packets = 0
		self.Load = 0.0

class endptState:
	"""
	a endptState holds extra informat used by the endpt
	"""
	def __init__(self):
		self.Rngstrm = None  # pointer to a random number generator
		self.Trace = False  # switch for calling add trace
		self.Drop = False  # whether to support packet drops at interface
		self.Active = {}  # map[int]float64
		self.Load = 0.0
		self.Forward = {}  # DFS
		self.Packets = 0

		self.InterruptDelay = 0.0

		self.BckgrndRate = 0.0
		self.BckgrndSrv = 0.0
		self.BckgrndIdx = 0

# a endptDev holds information about a endpt
class endptDev:
	def __init__(self):
		self.EndptName: str = ""			   # unique name
		self.EndptGroups: list[str] = []	   # list of groups to which endpt belongs
		self.EndptModel: str = ""			  # model of CPU the endpt uses
		self.EndptCores: int = 0			   # number of CPU cores
		self.EndptSched: TaskScheduler = None  					# shares an endpoint's cores among computing tasks
		self.EndptAccelSched: dict[str, TaskScheduler] = {}	 # map of accelerators, indexed by type name
		self.EndptAccelModel: dict[str, str] = {}				# accel device name on endpoint mapped to device model for timing
		self.EndptID: int = 0				  					# unique integer id
		self.EndptIntrfcs: list[intrfcStruct] = []  # list of network interfaces embedded in the endpt
		self.EndptState: endptState = None   		# a struct holding endpt state

	# matchParam is for other paramObj objects a method for seeing whether
	# the device attribute matches the input.  'cept the endptDev is not declared
	# to have any such attributes, so this function (included to let endptDev be
	# a paramObj) returns false.  Included to allow endptDev to satisfy paramObj interface requirements
	def match_param(self, attrb_name: str, attrb_value: str) -> bool:
		switch = {
			"name": lambda: self.EndptName == attrb_value,
			"group": lambda: attrb_value in self.EndptGroups,
			"model": lambda: self.EndptModel == attrb_value,
		}
		return switch.get(attrb_name, lambda: False)()

	# setParam gives a value to a endptDev parameter.  The design allows only
	# the CPU model parameter to be set, which is allowed here
	def set_param(self, param: str, value):
		if param == "trace":
			self.EndptState.Trace = (value.intValue == 1)
		elif param == "model":
			self.EndptModel = value.stringValue
		elif param == "cores":
			self.EndptCores = value.intValue
		elif param == "interruptdelay":
			# provided in musecs, so scale
			self.EndptState.InterruptDelay = value.floatValue * 1e-6
		elif param == "bckgrndSrv":
			self.EndptState.BckgrndSrv = value.floatValue
		elif param == "bckgrndRate":
			self.EndptState.BckgrndRate = value.floatValue

	# paramObjName helps endptDev satisfy paramObj interface, returns the endpt's name
	def param_obj_name(self) -> str:
		return self.EndptName

	# createEndptDev is a constructor, using information from the desc description of the endpt
	@staticmethod # TODO: define a few things here
	def create_endpt_dev(endpt_desc: EndptDesc) -> endptDev:
		endpt = endptDev()
		endpt.EndptName = endpt_desc.Name  # unique name
		endpt.EndptModel = endpt_desc.Model
		endpt.EndptCores = endpt_desc.Cores
		endpt.EndptID = nxtID()  # unique integer id, generated at model load-time
		endpt.EndptState = endptDev.create_endpt_state(endpt.EndptName)
		endpt.EndptIntrfcs = []  # initialization of list of interfaces, to be augmented later
		
		endpt.EndptGroups = list(endpt_desc.Groups)
		endpt.EndptAccelSched = {}
		
		endpt.EndptAccelModel = {}
		
		for accel_code, accel_model in endpt_desc.Accel.items():
			# if there is a "," split out the accelerator name and number of cores
			if "," in accel_code:
				pieces = accel_code.split(",")
				accel_name = pieces[0]
				try:
					accel_cores = int(pieces[1])
				except Exception:
					accel_cores = 1
			else:
				accel_name = accel_code
				accel_cores = 1
			endpt.EndptAccelModel[accel_name] = accel_model
			endpt.EndptAccelSched[accel_name] = CreateTaskScheduler(accel_cores)
		AccelSchedulersByHostName[endpt.EndptName] = endpt.EndptAccelSched
		return endpt

	# createEndptState constructs the data for the endpoint state
	@staticmethod
	def create_endpt_state(name: str):
		eps = endptState()
		eps.Active = {}
		eps.Load = 0.0
		eps.Packets = 0
		eps.Trace = False
		eps.Forward = {} # DFS (int->intrfcIDPair)
		eps.Rngstrm = rngstream.New(name)
		# default, nothing happens in the background
		eps.BckgrndRate = 0.0
		eps.BckgrndSrv = 0.0
		return eps

	# initTaskScheduler calls CreateTaskScheduler to create
	# the logic for incorporating the impacts of parallel cores
	# on execution time
	def init_task_scheduler(self):
		scheduler = CreateTaskScheduler(self.EndptCores) # TODO: scheduler.py
		self.EndptSched = scheduler
		# remove if already present
		TaskSchedulerByHostName.pop(self.EndptName, None)
		TaskSchedulerByHostName[self.EndptName] = scheduler

	# addIntrfc appends the input intrfcStruct to the list of interfaces embedded in the endpt.
	def add_intrfc(self, intrfc):
		self.EndptIntrfcs.append(intrfc)

	# CPUModel returns the string type description of the CPU model running the endpt
	def cpu_model(self) -> str:
		return self.EndptModel

	# DevName returns the endpt name, as part of the TopoDev interface
	def dev_name(self) -> str:
		return self.EndptName

	# DevID returns the endpt integer id, as part of the TopoDev interface
	def dev_id(self) -> int:
		return self.EndptID

	# DevType returns the endpt's device type, as part of the TopoDev interface
	def dev_type(self) -> DevCode:
		return DevCode.EndptCode

	# DevModel returns the endpt's model, as part of the TopoDev interface
	def dev_model(self) -> str:
		return self.EndptModel

	# DevIntrfcs returns the endpt's list of interfaces, as part of the TopoDev interface
	def dev_intrfcs(self):
		return self.EndptIntrfcs

	# DevState returns the endpt's state struct, as part of the TopoDev interface
	def dev_state(self):
		return self.EndptState

	def dev_forward(self):
		return self.EndptState.Forward

	# devRng returns the endpt's rng pointer, as part of the TopoDev interface
	def dev_rng(self):
		return self.EndptState.Rngstrm

	def log_net_event(self, time, nm, op):
		if not self.EndptState.Trace:
			return
		
		AddNetTrace(devTraceMgr, time, nm, self.EndptID, op)

	# DevAddActive adds an active connection, as part of the TopoDev interface.  Not used for endpts, yet
	def dev_add_active(self, nme: NetworkMsg):
		self.EndptState.Active[nme.ConnectID] = nme.Rate

	# DevRmActive removes an active connection, as part of the TopoDev interface.  Not used for endpts, yet
	def dev_rm_active(self, connect_id: int):
		self.EndptState.Active.pop(connect_id, None)

	# DevDelay returns the state-dependent delay for passage through the device, as part of the TopoDev interface.
	# Not really applicable to endpt, so zero is returned
	def dev_delay(self, msg: NetworkMsg) -> float:
		return 0.0


# The switchState struct holds auxiliary information about the switch
class switchState:
	def __init__(self):
		self.Rngstrm: 'rngstream.RngStream' = None   # pointer to a random number generator
		self.Trace: bool = False					# switch for calling trace saving
		self.Drop: bool = False					 # switch to allow dropping packets
		self.Active: dict[int, float] = {}		  # map of active connection IDs to rates
		self.Load: float = 0.0
		self.BufferSize: float = 0.0
		self.Capacity: float = 0.0
		self.Forward: DFS = {}					  # forwarding table
		self.Packets: int = 0
		self.DefaultOp: dict[str, str] = {}		 # default operation per source device
		self.DevExecOpTbl: dict[str, OpMethod] = {} # operation method table

# The switchDev struct holds information describing a run-time representation of a switch
class switchDev:
	def __init__(self):
		self.SwitchName: str = ""				  	# unique name
		self.SwitchGroups: list[str] = []		   # groups to which the switch may belong
		self.SwitchModel: str = ""				 	# model name, used to identify performance characteristics
		self.SwitchID: int = 0					  # unique integer id, generated at model-load time
		self.SwitchIntrfcs: list[intrfcStruct] = [] # list of network interfaces embedded in the switch
		self.SwitchState: switchState = None		# pointer to the switch's state struct

	# createSwitchDev is a constructor, initializing a run-time representation of a switch from its desc description
	@staticmethod
	def create_switch_dev(switch_desc: 'SwitchDesc') -> 'switchDev':
		swtch = switchDev()
		swtch.SwitchName = switch_desc.Name
		swtch.SwitchModel = switch_desc.Model
		swtch.SwitchID = nxtID()
		swtch.SwitchIntrfcs = []
		swtch.SwitchGroups = switch_desc.Groups
		swtch.SwitchState = switchDev.create_switch_state(switch_desc)
		return swtch

	# createSwitchState constructs data structures for the switch's state
	@staticmethod
	def create_switch_state(switch_desc: 'SwitchDesc') -> switchState:
		ss = switchState()
		ss.Active = {}
		ss.Load = 0.0
		ss.Packets = 0
		ss.Trace = False
		ss.Drop = False
		ss.Rngstrm = rngstream.New(switch_desc.Name)
		ss.Forward = {}
		ss.DevExecOpTbl = {}
		ss.DefaultOp = {}
		for src in switch_desc.OpDict:
			op = switch_desc.OpDict[src]
			ss.DefaultOp[src] = op

			# use this initialization to test against in DevDelay
			ss.DevExecOpTbl[op] = None
		return ss

	# DevForward returns the switch's forward table
	def dev_forward(self) -> DFS:
		return self.SwitchState.Forward

	# addForward adds an ingress/egress pair to the switch's forwarding table for a flowID
	def add_forward(self, flow_id: int, idp: intrfcIDPair):
		self.SwitchState.Forward[flow_id] = idp

	# rmForward removes a flowID from the switch's forwarding table
	def rm_forward(self, flow_id: int):
		self.SwitchState.Forward.pop(flow_id, None)

	def add_dev_exec_op(self, op: str, op_func: OpMethod):
		self.SwitchState.DevExecOpTbl[op] = op_func

	def set_default_op(self, src: str, op: str):
		self.SwitchState.DefaultOp[src] = op

	# matchParam is used to determine whether a run-time parameter description
	# should be applied to the switch. Its definition here helps switchDev satisfy
	# the paramObj interface.  To apply or not to apply depends in part on whether the
	# attribute given matchParam as input matches what the switch has. 'model' is the
	# only attribute we use to match a switch
	def match_param(self, attrb_name: str, attrb_value: str) -> bool:
		switch = {
			"name": lambda: self.SwitchName == attrb_value,
			"group": lambda: attrb_value in self.SwitchGroups,
			"model": lambda: self.SwitchModel == attrb_value,
		}
		return switch.get(attrb_name, lambda: False)()

	# setParam gives a value to a switchDev parameter, to help satisfy the paramObj interface.
	# Parameters that can be altered on a switch are "model", "execTime", and "buffer"
	def set_param(self, param: str, value: valueStruct): # TODO: param.py
		if param == "model":
			self.SwitchModel = value.stringValue
		elif param == "buffer":
			self.SwitchState.BufferSize = value.floatValue
		elif param == "trace":
			self.SwitchState.Trace = value.boolValue
		elif param == "drop":
			self.SwitchState.Drop = value.boolValue

	# paramObjName returns the switch name, to help satisfy the paramObj interface.
	def param_obj_name(self) -> str:
		return self.SwitchName

	# addIntrfc appends the input intrfcStruct to the list of interfaces embedded in the switch.
	def add_intrfc(self, intrfc: 'intrfcStruct'):
		self.SwitchIntrfcs.append(intrfc)

	# DevName returns the switch name, as part of the TopoDev interface
	def dev_name(self) -> str:
		return self.SwitchName

	# DevID returns the switch integer id, as part of the TopoDev interface
	def dev_id(self) -> int:
		return self.SwitchID

	# DevType returns the switch's device type, as part of the TopoDev interface
	def dev_type(self) -> 'DevCode':
		return DevCode.SwitchCode

	# DevModel returns the switch's model, as part of the TopoDev interface
	def dev_model(self) -> str:
		return self.SwitchModel

	# DevIntrfcs returns the switch's list of interfaces, as part of the TopoDev interface
	def dev_intrfcs(self) -> list['intrfcStruct']:
		return self.SwitchIntrfcs

	# DevState returns the switch's state struct, as part of the TopoDev interface
	def dev_state(self) -> switchState:
		return self.SwitchState

	# DevRng returns the switch's rng pointer, as part of the TopoDev interface
	def dev_rng(self) -> 'rngstream.RngStream':
		return self.SwitchState.Rngstrm

	# DevAddActive adds a connection id to the list of active connections through the switch, as part of the TopoDev interface
	def dev_add_active(self, nme: 'NetworkMsg'):
		self.SwitchState.Active[nme.ConnectID] = nme.Rate

	# devRmActive removes a connection id to the list of active connections through the switch, as part of the TopoDev interface
	def dev_rm_active(self, connect_id: int):
		self.SwitchState.Active.pop(connect_id, None)

	# devDelay returns the state-dependent delay for passage through the switch, as part of the TopoDev interface.
	def dev_delay(self, msg: 'NetworkMsg') -> float:
		# if the switch doesn't do anything non-default just do the default switch
		if len(self.SwitchState.DevExecOpTbl) == 0:
			return DelayThruDevice(self.SwitchModel, DefaultSwitchOp, msg.MsgLen)
		
		# look for a match in the keys of the message metadata dictionary and
		# keys in the switch DevExecOpTbl.  On a match, call the function in the table
		# and pass to it the meta data
		for meta_key in msg.MetaData:
			op_func = self.SwitchState.DevExecOpTbl.get(meta_key)
			if op_func is not None:

				# see if the function is actually the empty one, meaning its not there
				# TODO: see if this is necessary
				# if op_func is None:
				# 	raise Exception(f"in switch {self.SwitchName} dev op {meta_key} lacking user-provided instantiation")
				
				return op_func(self, meta_key, msg)
		
		# didn't find a match, so see if there is a default operation listed for traffic
		# from the previous device
		msg_src_name = prevDeviceName(msg)
		default_op = self.SwitchState.DefaultOp.get(msg_src_name)
		if default_op is not None:
			op_func = self.SwitchState.DevExecOpTbl.get(default_op)
			if op_func is None:
				raise Exception(f"in switch {self.SwitchName} dev op {default_op} lacking user-provided instantiation")
			
			return op_func(self, default_op, msg)
		
		# no user-defined default listed, so use system default
		return DelayThruDevice(self.SwitchModel, DefaultSwitchOp, msg.MsgLen)

	# LogNetEvent satisfies TopoDev interface
	def log_net_event(self, time: 'vrtime.Time', nm: 'NetworkMsg', op: str):
		if not self.SwitchState.Trace:
			return
		AddNetTrace(devTraceMgr, time, nm, self.SwitchID, op)

# The routerState type describes auxiliary information about the router
class routerState:
	def __init__(self):
		self.Rngstrm: 'rngstream.RngStream' = None   # pointer to a random number generator
		self.Trace: bool = False					# switch for calling trace saving
		self.Drop: bool = False					 # switch for allowing packet drops
		self.Active: dict[int, float] = {}
		self.Load: float = 0.0
		self.Buffer: float = float('inf')		   # default to a very large buffer
		self.Forward: dict[int, 'intrfcIDPair'] = {}
		self.Packets: int = 0
		self.DefaultOp: dict[str, str] = {}
		self.DevExecOpTbl: dict[str, 'OpMethod'] = {}

# The routerDev struct holds information describing a run-time representation of a router
class routerDev:
	def __init__(self):
		self.RouterName: str = ""				  # unique name
		self.RouterGroups: list[str] = []		   # list of groups to which the router belongs
		self.RouterModel: str = ""				 # attribute used to identify router performance characteristics
		self.RouterID: int = 0					  # unique integer id assigned at model-load time
		self.RouterIntrfcs: list['intrfcStruct'] = [] # list of interfaces embedded in the router
		self.RouterState: 'routerState' = None	  # pointer to the struct of the routers auxiliary state

	# createRouterDev is a constructor, initializing a run-time representation of a router from its desc representation
	@staticmethod
	def create_router_dev(router_desc: 'RouterDesc') -> 'routerDev':
		router = routerDev()
		router.RouterName = router_desc.Name
		router.RouterModel = router_desc.Model
		router.RouterID = nxtID()
		router.RouterIntrfcs = []
		router.RouterGroups = router_desc.Groups # TODO: is this a copy or reference?
		router.RouterState = routerDev.create_router_state(router_desc)
		return router
	
	# createRouterState is a constructor, initializing the State dictionary for a router
	@staticmethod
	def create_router_state(router_desc: 'RouterDesc') -> 'routerState':
		rs = routerState()
		rs.Active = {}
		rs.Load = 0.0
		rs.Buffer = float('inf') / 2.0
		rs.Packets = 0
		rs.Trace = False
		rs.Drop = False
		rs.Rngstrm = rngstream.New(router_desc.Name)
		rs.Forward = {}
		rs.DevExecOpTbl = {}
		rs.DefaultOp = {}
		for src in router_desc.OpDict:
			op = router_desc.OpDict[src]
			rs.DefaultOp[src] = op
			
			# use this initialization to test against in DevDelay
			rs.DevExecOpTbl[op] = None
		return rs

	# DevForward returns the router's forwarding table
	def dev_forward(self) -> 'DFS':
		return self.RouterState.Forward

	# addForward adds an flowID entry to the router's forwarding table
	def add_forward(self, flow_id: int, idp: 'intrfcIDPair'):
		self.RouterState.Forward[flow_id] = idp

	# rmForward removes a flowID from the router's forwarding table
	def rm_forward(self, flow_id: int):
		self.RouterState.Forward.pop(flow_id, None)

	def add_dev_exec_op(self, op: str, op_func: 'OpMethod'):
		self.RouterState.DevExecOpTbl[op] = op_func

	def set_default_op(self, src: str, op: str):
		self.RouterState.DefaultOp[src] = op

	# matchParam is used to determine whether a run-time parameter description
	# should be applied to the router. Its definition here helps switchDev satisfy
	# the paramObj interface.  To apply or not to apply depends in part on whether the
	# attribute given matchParam as input matches what the router has. 'model' is the
	# only attribute we use to match a router
	def match_param(self, attrb_name: str, attrb_value: str) -> bool:
		if attrb_name == "name":
			return self.RouterName == attrb_value
		elif attrb_name == "group":
			return attrb_value in self.RouterGroups
		elif attrb_name == "model":
			return self.RouterModel == attrb_value
		
		# an error really, as we should match only the names given in the switch statement above
		return False

	# setParam gives a value to a routerDev parameter, to help satisfy the paramObj interface.
	# Parameters that can be altered on a router are "model", "execTime", and "buffer"
	def set_param(self, param: str, value: 'valueStruct'):
		if param == "model":
			self.RouterModel = value.stringValue
		elif param == "buffer":
			self.RouterState.Buffer = value.floatValue
		elif param == "trace":
			self.RouterState.Trace = value.boolValue
		elif param == "drop":
			self.RouterState.Drop = value.boolValue

	# paramObjName returns the router name, to help satisfy the paramObj interface.
	def param_obj_name(self) -> str:
		return self.RouterName

	# addIntrfc appends the input intrfcStruct to the list of interfaces embedded in the router.
	def add_intrfc(self, intrfc: 'intrfcStruct'):
		self.RouterIntrfcs.append(intrfc)

	# DevName returns the router name, as part of the TopoDev interface
	def dev_name(self) -> str:
		return self.RouterName

	# DevID returns the switch integer id, as part of the TopoDev interface
	def dev_id(self) -> int:
		return self.RouterID

	# DevType returns the router's device type, as part of the TopoDev interface
	def dev_type(self) -> 'DevCode':
		return DevCode.RouterCode

	# DevModel returns the endpt's model, as part of the TopoDev interface
	def dev_model(self) -> str:
		return self.RouterModel

	# DevIntrfcs returns the routers's list of interfaces, as part of the TopoDev interface
	def dev_intrfcs(self) -> list['intrfcStruct']:
		return self.RouterIntrfcs

	# DevState returns the routers's state struct, as part of the TopoDev interface
	def dev_state(self) -> 'routerState':
		return self.RouterState

	# DevRng returns a pointer to the routers's rng struct, as part of the TopoDev interface
	def dev_rng(self) -> 'rngstream.RngStream':
		return self.RouterState.Rngstrm

	# LogNetEvent includes a network trace report
	def log_net_event(self, time: 'vrtime.Time', nm: 'NetworkMsg', op: str):
		if not self.RouterState.Trace:
			return
		
		AddNetTrace(devTraceMgr, time, nm, self.RouterID, op)

	# DevAddActive includes a connectID as part of what is active at the device, as part of the TopoDev interface
	def dev_add_active(self, nme: 'NetworkMsg'):
		self.RouterState.Active[nme.ConnectID] = nme.Rate

	# DevRmActive removes a connectID as part of what is active at the device, as part of the TopoDev interface
	def dev_rm_active(self, connect_id: int):
		self.RouterState.Active.pop(connect_id, None)


	# devDelay returns the state-dependent delay for passage through the router, as part of the TopoDev interface.
	def dev_delay(self, msg: 'NetworkMsg') -> float:
		# if the router doesn't do anything non-default just do the default router
		if len(self.RouterState.DevExecOpTbl) == 0:
			return DelayThruDevice(self.RouterModel, DefaultRouteOp, msg.MsgLen)
		
		# look for a match in the keys of the message metadata dictionary and
		# keys in the router DevExecOpTbl.  On a match, call the function in the table
		# and pass to it the meta data
		for meta_key in msg.MetaData:
			op_func = self.RouterState.DevExecOpTbl.get(meta_key)
			if op_func is not None:

				# if op_func is None:
				# 	raise Exception(f"in router {self.RouterName} dev op {meta_key} lacking user-provided instantiation")
				
				return op_func(self, meta_key, msg)
		
		# didn't find a match, so see if there is a default operation listed for traffic
		# from the previous device
		msg_src_name = prevDeviceName(msg)
		default_op = self.RouterState.DefaultOp.get(msg_src_name)
		if default_op is not None:
			op_func = self.RouterState.DevExecOpTbl.get(default_op)
			if op_func is None:
				raise Exception(f"in router {self.RouterName} dev op {default_op} lacking user-provided instantiation")
			return op_func(self, "", msg)
		
		# no user-defined default listed, so use system default
		return DelayThruDevice(self.RouterModel, DefaultRouteOp, msg.MsgLen)


# The intrfcsToDev struct describes a connection to a device.  Used in route descriptions
class intrfcsToDev:
	def __init__(self, srcIntrfcID: int, dstIntrfcID: int, netID: int, devID: int):
		self.srcIntrfcID: int = srcIntrfcID  # id of the interface where the connection starts
		self.dstIntrfcID: int = dstIntrfcID  # id of the interface embedded by the target device
		self.netID: int = netID			  # id of the network between the src and dst interfaces
		self.devID: int = devID			  # id of the device the connection targets

NetworkMsgID: int = 1

# The NetworkMsg type creates a wrapper for a message between comp pattern funcs.
# One value (StepIdx) indexes into a list of route steps, so that by incrementing
# we can find 'the next' step in the route.  One value is a pointer to this route list,
# and the final value is a pointer to an inter-func comp pattern message.
class NetworkMsg:
	def __init__(self):
		self.MsgID: int = 0				 # ID created message is delivered to network
		self.MsrID: int = 0				 # measure identity ID from message delivered to network
		self.StepIdx: int = 0			   # position within the route from source to destination
		self.Route: list[intrfcsToDev] = [] # pointer to description of route
		self.Connection: 'ConnDesc' = None  # {DiscreteConn, MajorFlowConn, MinorFlowConn}
		self.ExecID: int = 0
		self.FlowID: int = 0				# flow id given by app at entry
		self.ConnectID: int = 0			 # connection identifier
		self.NetMsgType: 'NetworkMsgType' = None # enum type packet,
		self.PcktRate: float = 0.0		  # rate from source
		self.Rate: float = 0.0			  # flow rate if FlowID >0 (meaning a flow)
		self.Syncd: list[int] = []		  # IDs of flows with which this message has synchronized
		self.StartTime: float = 0.0		 # simulation time when the message entered the network
		self.PrArrvl: float = 0.0		   # probability of arrival
		self.MsgLen: int = 0				# length of the entire message, in bytes
		self.PcktIdx: int = 0			   # index of packet with msg
		self.NumPckts: int = 0			  # number of packets in the message this is part of
		self.MetaData: dict[str, any] = {}  # carrier of extra stuff
		self.Msg: any = None				# message being carried.
		self.intrfcArr: float = 0.0
		self.StrmPckt: bool = False
		self.prevIntrfcID: int = 0		  # ID of interface through which message last passed
		self.prevIntrfcSend: float = 0.0	# time at which message left previous interface

	# carriesPckt returns a boolean indicating whether the message is a packet (verses flow)
	def carries_pckt(self) -> bool:
		return self.NetMsgType == NetworkMsgType.PacketType

# Pt2ptLatency computes the latency on a point-to-point connection
# between interfaces.  Called when neither interface is attached to a router
def pt2pt_latency(src_intrfc: 'intrfcStruct', dst_intrfc: 'intrfcStruct') -> float:
	return max(src_intrfc.State.Latency, dst_intrfc.State.Latency)

# currentIntrfcs returns pointers to the source and destination interfaces whose
# id values are carried in the current step of the route followed by the input argument NetworkMsg.
# If the interfaces are not directly connected but communicate through a network,
# a pointer to that network is returned also
def current_intrfcs(nm: NetworkMsg) -> tuple['intrfcStruct', 'intrfcStruct', 'networkStruct']:
	src_intrfc_id = nm.Route[nm.StepIdx].srcIntrfcID
	dst_intrfc_id = nm.Route[nm.StepIdx].dstIntrfcID

	src_intrfc = IntrfcByID[src_intrfc_id]
	dst_intrfc = IntrfcByID[dst_intrfc_id]

	net_id = nm.Route[nm.StepIdx].netID
	if net_id != -1:
		ns = NetworkByID[net_id]
	else:
		ns = NetworkByID[commonNetID(src_intrfc, dst_intrfc)]

	return src_intrfc, dst_intrfc, ns

# transitDelay returns the length of time (in seconds) taken
# by the input argument NetworkMsg to traverse the current step in the route it follows.
# That step may be a point-to-point wired connection, or may be transition through a network
# w/o specification of passage through specific devices. In addition a pointer to the
# network transited (if any) is returned
def transit_delay(nm: NetworkMsg) -> tuple[float, 'networkStruct']:
	delay = 0.0
	src_intrfc, dst_intrfc, net = current_intrfcs(nm)

	if (src_intrfc.Cable is not None and dst_intrfc.Cable is None) or \
	   (src_intrfc.Cable is None and dst_intrfc.Cable is not None):
		raise Exception("cabled interface confusion")

	if src_intrfc.Cable is None:
		# delay is through network (baseline)
		delay = net.NetLatency(nm)
	else:
		# delay is across a pt-to-pt line
		delay = pt2pt_latency(src_intrfc, dst_intrfc)
	return delay, net

lastEntrance: float = 0.0
interdist: dict[int, int] = {}
NumArrivals: int = 0
Foreground: int = 0

StrmLags: int = 0
FgLags: int = 0

# enterEgressIntrfc handles the arrival of a packet to the egress interface side of a device.
# It will have been forwarded from the device if an endpoint, or from the ingress interface
def enter_egress_intrfc(evt_mgr: 'evtm.EventManager', egress_intrfc: any, msg: any) -> any:
	# cast context argument to interface
	intrfc = egress_intrfc  # assume already an intrfcStruct

	# cast data argument to network message. Notice that the entire message is passed, not just a pointer to it.
	nm = msg  # assume already a NetworkMsg

	now_in_secs = roundFloat(evt_mgr.CurrentSeconds(), rdigits) # TODO: flow-sim.go
	
	# record arrival
	intrfc.addTrace("enterEgressIntrfc", nm, now_in_secs)
	
	AddIntrfcTrace(devTraceMgr, evt_mgr.CurrentTime(), nm.ExecID, nm.MsgID,
		intrfc.Number, "enterEgress", intrfc.State.EgressIntrfcQ.Str())
	
	# note the addition of a network message to the egress queue.
	# This will cause any scheduling that is needed for passing to the exit step of the egress interface
	new_msg = NetworkMsg()
	new_msg.__dict__.update(nm.__dict__) # TODO: what is this doing?
	intrfc.State.EgressIntrfcQ.addNetworkMsg(evt_mgr, new_msg)
	return None

# advance has already just been called on the strmSet
def sched_nxt_msg_to_intrfc(evt_mgr: 'evtm.EventManager', intrfc_q: 'intrfcQStruct'):
	# bring in next message across egress interface, if present
	current_time = evt_mgr.CurrentSeconds()
	
	# are there foreground messages waiting to be processed?
	if len(intrfc_q.msgQueue) > 0:
		if intrfc_q.intrfc.Name == "intrfc@hubWest-rtr" and not intrfc_q.ingress:
			msrArrivals = True
		
		nxt_msg = intrfc_q.msgQueue[0].nm
		service_time = computeServiceTime(nxt_msg.MsgLen, intrfc_q.intrfc.State.Bndwdth)
		advanced, q_delay = intrfc_q.strmQ.queueingDelay(current_time, nxt_msg.intrfcArr, service_time, nxt_msg.prevIntrfcID)
		
		msrArrivals = False
		
		if advanced:
			evt_mgr.Schedule(intrfc_q, nxt_msg.MsgID, enter_intrfc_service, vrtime.SecondsToTime(q_delay))
		else:
			enter_intrfc_service(evt_mgr, intrfc_q, nxt_msg.MsgID)

# exitEgressIntrfc implements an event handler for the completed departure of a message from an interface.
# It determines the time-through-network and destination of the message, and schedules the recognition
# of the message at the ingress interface
def exit_egress_intrfc(evt_mgr: 'evtm.EventManager', egress_intrfc: any, data: any) -> any:
	intrfc = egress_intrfc
	intrfc.State.EgressTransit = False
	intrfc_q = intrfc.State.EgressIntrfcQ
	
	current_time = evt_mgr.CurrentSeconds()
	
	nm = data
	
	if nm is None:
		raise Exception("popped empty network message queue")
	
	# the time through the interface has already been accounted for.
	# we need to add the time through the network, and schedule delivery 
	# of the message to the ingress interface after that delay.
	nxt_intrfc = IntrfcByID[nm.Route[nm.StepIdx].dstIntrfcID]
	
	# create a vrtime representation of when service is completed that starts now
	vt_depart_time = vrtime.SecondsToTime(current_time)
	
	# record that time in logs
	intrfc.PrmDev.log_net_event(vt_depart_time, nm, "exit")
	intrfc.log_net_event(vt_depart_time, nm, "exit")
	
	AddIntrfcTrace(devTraceMgr, evt_mgr.CurrentTime(), nm.ExecID, nm.MsgID, intrfc.Number,
		"exitEgress", intrfc.State.EgressIntrfcQ.Str())
	
	# remember that we visited
	intrfc.addTrace("exitEgressIntrfc", nm, current_time)
	
	net_delay, net = transit_delay(nm)
	
	# log the completed departure
	net.log_net_event(vt_depart_time, nm, "enter")
	
	# alignment is for debugging only
	# alignment = align_service_time(nxt_intrfc, roundFloat(current_time+net_delay, rdigits), nm.MsgLen)
	
	priority = int(intrfc.Device.dev_rng().RandInt(1, 1000000))
	
	# remember that we passed through this interface
	nm.prevIntrfcID = intrfc.Number
	
	# pass time of last message from this interface to the destination,
	# as this will be used at next interface for constructing
	# strm arrival times that are synchronized with the foreground packet sends
	evt_mgr.Schedule(nxt_intrfc, nm, enter_ingress_intrfc, vrtime.SecondsToTimePri(net_delay, priority))
	
	sched_nxt_msg_to_intrfc(evt_mgr, intrfc_q)
	
	# event-handlers are required to return _something_
	return None

# enterIngressIntrfc implements the event-handler for the entry of a frame
# to an interface through which the frame will pass on its way into a device. The
# event time is the arrival of the frame, does not yet include load dependent delays
def enter_ingress_intrfc(evt_mgr: 'evtm.EventManager', ingress_intrfc: any, msg: any) -> any:
	intrfc = ingress_intrfc
	
	# cast data argument to network message
	nm = msg
	
	now_in_secs = evt_mgr.CurrentSeconds()
	
	# record arrival
	intrfc.addTrace("enterIngressIntrfc", nm, now_in_secs)
	
	AddIntrfcTrace(devTraceMgr, evt_mgr.CurrentTime(), nm.ExecID, nm.MsgID,
		intrfc.Number, "enterIngress", intrfc.State.IngressIntrfcQ.Str())
	
	# note the addition of a network message to the ingress queue.
	# This will cause any scheduling that is needed for passing to the exit step of the ingress interface
	new_msg = NetworkMsg()
	new_msg.__dict__.update(nm.__dict__) # TODO: is this a correct copy?
	intrfc.State.IngressIntrfcQ.addNetworkMsg(evt_mgr, new_msg)
	return None

def align_service_time(intrfc: 'intrfcStruct', time: float, msg_len: int) -> float:
	service_time = roundFloat(float(msg_len*8)/(intrfc.State.Bndwdth*1e6), rdigits)
	ticks = int(time / service_time)
	if abs(float(ticks)*service_time-time) < eqlThrsh:
		return 0.0
	
	return roundFloat(float(ticks+1)*service_time-time, rdigits) # TODO: flow-sim.py

def arrive_ingress_intrfc(evt_mgr: 'evtm.EventManager', ingress_intrfc: any, msg: any) -> any:
	intrfc = ingress_intrfc
	intrfc.State.IngressTransit = False
	intrfc_q = intrfc.State.IngressIntrfcQ
	current_time = evt_mgr.CurrentSeconds()
	nm = msg
	
	# remember that we last passed through this interface
	nm.prevIntrfcID = intrfc.Number
	
	intrfc.log_net_event(evt_mgr.CurrentTime(), nm, "enter")
	intrfc.PrmDev.log_net_event(evt_mgr.CurrentTime(), nm, "enter")
	AddIntrfcTrace(devTraceMgr, evt_mgr.CurrentTime(), nm.ExecID, nm.MsgID, intrfc.Number,
		"arriveIngress", intrfc.State.IngressIntrfcQ.Str())
	net_dev_type = intrfc.Device.dev_type()
	
	# cast data argument to network message
	intrfc.addTrace("arriveIngressIntrfc", nm, current_time)
	
	# if there is a message waiting for transmission, bring it
	sched_nxt_msg_to_intrfc(evt_mgr, intrfc_q)
	
	# if this device is an endpoint, the message gets passed up to it
	device = intrfc.Device
	dev_code = device.dev_type()
	
	nmbody = nm
	if dev_code == DevCode.EndptCode:
		# schedule return into comp pattern system, where requested
		ActivePortal.Depart(evt_mgr, device.dev_name(), nmbody)
		return None
	
	# estimate the probability of dropping the packet on the way out
	if net_dev_type == DevCode.RouterCode or net_dev_type == DevCode.SwitchCode:
		nxt_intrfc = IntrfcByID[nmbody.Route[nm.StepIdx+1].srcIntrfcID]
		buffer = nxt_intrfc.State.BufferSize						 # buffer size in Mbytes
		N = int(round(buffer * 1e+6 / float(nmbody.MsgLen)))		 # buffer length in Mbytes
		lambda_ = intrfc.State.IngressLambda
		pr_drop = estPrDrop(lambda_, intrfc.State.Bndwdth, N)
		nmbody.PrArrvl *= (1.0 - pr_drop)
	
	# get the delay through the device
	delay = device.dev_delay(nmbody)
	delay = roundFloat(delay, rdigits)
	
	# advance position along route
	nmbody.StepIdx += 1
	nxt_intrfc = IntrfcByID[nmbody.Route[nmbody.StepIdx].srcIntrfcID]
	
	# statement below is just for debugging
	# alignment = align_service_time(nxt_intrfc, roundFloat(current_time+delay, rdigits), nmbody.MsgLen)
	priority = int(intrfc.Device.dev_rng().RandInt(1, 1000000))
	evt_mgr.Schedule(nxt_intrfc, nmbody, enter_egress_intrfc, vrtime.SecondsToTimePri(delay, priority))
	return None


SumWaits: float = 0.0
NumWaits: int = 0
ClientStrm: int = 0
StrmSamples: int = 0

# schedule the passage of the first message in the intrfcQStruct's queue
# through the interface, as a function of the message length and the interface bandwidth
def enter_intrfc_service(evt_mgr: 'evtm.EventManager', context: any, data: any) -> any:
	iqs = context  # assume already an intrfcQStruct
	ingress = iqs.ingress
	intrfc = iqs.intrfc
	msg_id = data  # assume already an int

	if iqs.ingress:
		intrfc.State.IngressTransit = True
		intrfc.State.IngressTransitMsgID = msg_id
	else:
		intrfc.State.EgressTransit = True
		intrfc.State.EgressTransitMsgID = msg_id

	# pop the message off the queue
	if ingress:
		nm = intrfc.State.IngressIntrfcQ.popNetworkMsg()
	else:
		nm = intrfc.State.EgressIntrfcQ.popNetworkMsg()

	# compute the delay through the interface at its bandwidth speed
	if nm.MsgID != msg_id:
		raise Exception("intrfcQ msgQueue ordering problem")

	msg_len_mbits = float(8 * nm.MsgLen) / 1e6
	latency = roundFloat(msg_len_mbits / intrfc.State.Bndwdth, rdigits)

	# schedule the message delivery on the 'other side' of the interface.
	# The event handler to schedule depends on whether the passage is egress or ingress
	priority = int(intrfc.Device.dev_rng().RandInt(1, 1000000))
	if ingress:
		evt_mgr.Schedule(intrfc, nm, arrive_ingress_intrfc, vrtime.SecondsToTimePri(latency, priority))
	else:
		evt_mgr.Schedule(intrfc, nm, exit_egress_intrfc, vrtime.SecondsToTimePri(latency, priority))

	return None

# estMM1NFull estimates the probability that an M/M/1/N queue is full.
# formula needs only the server utilization u, and the number of jobs in system, N
def est_mm1n_full(u: float, N: int) -> float:
	pr_full = 1.0 / float(N + 1)
	if abs(1.0 - u) < 1e-3:
		pr_full = (1 - u) * pow(u, float(N)) / (1 - pow(u, float(N + 1)))
	return pr_full

# estPrDrop estimates the probability that a packet is dropped passing through a network.
# From the function arguments it computes the server utilization and the number of
# messages that can arrive in the delay time, computes the probability
# of being in the 'full' state and returns that
def est_pr_drop(rate: float, capacity: float, N: int) -> float:
	u = rate / capacity
	
	# estimate number of packets that can be served as the
	# number that can be present over the latency time,
	# given the rate and message length.
	#
	# in Mbits a packet is 8 * msgLen bytes / (1e+6 bits/Mbbit) = (8*m/1e+6) Mbits
	#
	# with a bandwidth of rate Mbits/sec, the rate in pckts/sec is
	#
	#		Mbits
	#   rate ------
	#		 sec					  rate	 pckts
	#  -----------------------  =	 ------	----
	#				 Mbits		 (8*m/1e+6)   sec
	#	(8*m/1e+6)  ------
	#				  pckt
	#
	# The number of packets that can be accepted at this rate in a period of L secs is
	#
	#  L * rate
	#  -------- pckts
	#  (8*m/1e+6)
	#
	return est_mm1n_full(u, N)

# FrameSizeCache holds previously computed minimum frame size along the route
# for a flow whose ID is the index
FrameSizeCache: dict[int, int] = {}

# FindFrameSize traverses a route and returns the smallest MTU on any
# interface along the way.  This defines the maximum frame size to be
# used on that route.
def find_frame_size(frame_id: int, rt: list['intrfcsToDev']) -> int:
	if frame_id in FrameSizeCache:
		return FrameSizeCache[frame_id]
	
	frame_size = 1560
	for step in rt:
		src_intrfc = IntrfcByID[step.srcIntrfcID]
		src_frame_size = src_intrfc.State.MTU
		if src_frame_size > 0 and src_frame_size < frame_size:
			frame_size = src_frame_size
		dst_intrfc = IntrfcByID[step.dstIntrfcID]
		dst_frame_size = dst_intrfc.State.MTU
		if dst_frame_size > 0 and dst_frame_size < frame_size:
			frame_size = dst_frame_size
	FrameSizeCache[frame_id] = frame_size
	return frame_size

# DelayThruDevice computes the delay through a device given its model, op, and message length
def delay_thru_device(model: str, op: str, msg_len: int) -> float:
	if op not in devExecTimeTbl:
		raise Exception(f"dev op timing requested for unknown op {op} on model {model}")
	
	tbl = devExecTimeTbl[op][model]
	if model not in devExecTimeTbl[op] or len(tbl) == 0:
		raise Exception(f"dev op timing requested for unknown model {model} executing op {op}")
	x = dev_op_time_from_tbl(tbl, op, model, msg_len)

	opDesc = tbl[len(tbl) // 2]
	b = opDesc.bndwdth
	if(b <= 0.0):
		if op == "route":
			b = DefaultFloat["Router-bndwdth"]
		else:
			b = DefaultFloat["Switch-bndwdth"]

	d = 8.0 / (b * 1e6)
	p = float(msg_len)
	return max((x - 2 * p * d), 0.0)

devExecTimeCache: dict[str, dict[str, dict[int, float]]] = {}

# devOpTimeFromTbl estimates the execution time for a device operation
def dev_op_time_from_tbl(tbl: list['opTimeDesc'], op: str, model: str, msg_len: int) -> float:
	# get the parameters needed for the func execution time lookup
	if is_nop(op):
		return 0.0
	
	# check cache
	if op not in devExecTimeCache:
		devExecTimeCache[op] = {}
	
	if model not in devExecTimeCache[op]:
		devExecTimeCache[op][model] = {}
	
	if msg_len in devExecTimeCache[op][model]:
		return devExecTimeCache[op][model][msg_len]
	
	# the packetlen is not in the map and not in the cache
	#   if msgLen is zero find the smallest entry in the table
	# and call that the value for pcktlen 0.
	pls = [pl.pcktLen for pl in tbl]
	pls.sort()

	if msg_len == 0:
		value = tbl[0].execTime
		devExecTimeCache[op][model][0] = value
		return value
	
	#   not in the cache, not in the table.  Estimate based on
	#	 pcktLen relative to sorted list of known packet lengths
	#   case len(pls) = 1 --- estimate based on straight line from origin to pls[0]
	#   case: pcktLen < pls[0] and len(pls) > 1 --- use slope between pls[0] and pls[1]
	#   case: pls[0] <= pcktLen < pls[len(pls)-1] --- do a linear interpolation
	#   case: pls[len(pls)-1] < pcktLen and len(pls) > 1 --- use slope between last two points
	if len(pls) == 1:
		value = float(msg_len) * tbl[0].execTime / float(tbl[0].pcktLen)
		devExecTimeCache[op][model][msg_len] = value
		return value
	
	# there are at least two measurements, find the closest way to estimate
	# linear approximation
	left_idx = 0
	right_idx = 1
	if pls[-1] < msg_len:
		left_idx = len(pls) - 2
		right_idx = left_idx + 1
	else:
		if pls[0] <= msg_len and msg_len <= pls[-1]:
			while pls[right_idx] < msg_len:
				right_idx += 1
			left_idx = right_idx - 1
			
	dely = tbl[right_idx].execTime - tbl[left_idx].execTime
	delx = float(tbl[right_idx].pcktLen - tbl[left_idx].pcktLen)
	slope = dely / delx
	intercept = tbl[right_idx].execTime - slope * float(tbl[right_idx].pcktLen)
	value = intercept + slope * float(msg_len)
	devExecTimeCache[op][model][msg_len] = value
	return value

# isNOP returns True if the operation is a no-op
def is_nop(op: str) -> bool:
	return op in ["noop", "NOOP", "NOP", "no-op", "nop"]

# prevDeviceName returns the name of the previous device in the route
def prev_device_name(msg: 'NetworkMsg') -> str:
	route = msg.Route
	rt_step = route[msg.StepIdx]
	src_intrfc = IntrfcByID[rt_step.srcIntrfcID]
	return src_intrfc.Device.dev_name()
