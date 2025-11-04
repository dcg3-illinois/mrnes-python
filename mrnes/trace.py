"""
Python translation of mrnes/trace.go
Implements TraceManager and related trace record types for simulation tracing and serialization to JSON/YAML.

This module is a direct translation of Go's trace.go, including all comments and documentation.
It provides tracing infrastructure for simulation models, including trace records, trace manager, and serialization to JSON/YAML.
"""
import json
import yaml
import pathlib
from typing import Dict, List, Any
import evt_python.evt.vrtime as vrtime

class TraceRecordType:
    # TraceRecordType is an enum for trace record types
    NetworkType = 0  # NetworkType TraceRecordType = iota
    CmpPtnType = 1

class TraceInst:
    """
    TraceInst holds a single trace record, including time, type, and serialized string.
    """
    def __init__(self, TraceTime: str, TraceType: str, TraceStr: str):
        self.TraceTime = TraceTime
        self.TraceType = TraceType
        self.TraceStr = TraceStr

class NameType:
    """
    NameType is an entry in a dictionary created for a trace
    that maps object id numbers to a (name,type) pair
    """
    def __init__(self, Name: str, Type: str):
        self.Name = Name
        self.Type = Type

class TraceManager:
    """
    TraceManager implements the pces TraceManager interface. It is
    used to gather information about a simulation model and an execution of that model.
    """
    def __init__(self, ExpName: str, active: bool):
        # experiment uses trace
        self.InUse = active
        # name of experiment
        self.ExpName = ExpName
        # text name associated with each objID
        self.NameByID: Dict[int, NameType] = {}
        # all trace records for this experiment
        self.Traces: Dict[int, List[TraceInst]] = {}

    def active(self) -> bool:
        """
        Active tells the caller whether the Trace Manager is actively being used
        """
        return self.InUse

    def add_trace(self, vrt: Any, execID: int, trace: TraceInst):
        """
        AddTrace creates a record of the trace using its calling arguments, and stores it
        """
        # return if we aren't using the trace manager
        if not self.InUse:
            return
        if execID not in self.Traces:
            self.Traces[execID] = []
        self.Traces[execID].append(trace)

    def add_name(self, id: int, name: str, objDesc: str):
        """
        AddName is used to add an element to the id -> (name,type) dictionary for the trace file
        """
        if self.InUse:
            if id in self.NameByID:
                raise Exception("duplicated id in AddName")
            self.NameByID[id] = NameType(name, objDesc)

    def write_to_file(self, filename: str, globalOrder: bool) -> bool:
        """
        WriteToFile stores the Traces struct to the file whose name is given.
        Serialization to json or to yaml is selected based on the extension of this name.
        """
        if not self.InUse:
            return False
        
        path_ext = pathlib.Path(filename).suffix.lower()
        if not globalOrder:
            data = self._serialize()
        else:
            data = self._serialize_global_order()
        
        if path_ext in ['.yaml', '.yml']:
            bytestr = yaml.dump(data, sort_keys=False)
        elif path_ext == '.json':
            bytestr = json.dumps(data, indent=2)
        else:
            raise Exception(f"Unsupported file extension: {path_ext}")
        
        with open(filename, 'w') as f:
            f.write(bytestr)

        return True

    def _serialize(self):
        return {
            'inuse': self.InUse,
            'expname': self.ExpName,
            'namebyid': {k: vars(v) for k, v in self.NameByID.items()},
            'traces': {k: [vars(t) for t in v] for k, v in self.Traces.items()}
        }

    def _serialize_global_order(self):
        ntm = {
            'inuse': self.InUse,
            'expname': self.ExpName,
            'namebyid': {k: vars(v) for k, v in self.NameByID.items()},
            'traces': {}
        }
        all_traces = []
        for value_list in self.Traces.values():
            all_traces.extend(value_list)
        all_traces.sort(key=lambda t: float(t.TraceTime))
        ntm['traces'][0] = [vars(t) for t in all_traces]
        return ntm

class NetTrace:
    """
    NetTrace saves information about the visitation of a message to some point in the simulation.
    Saved for post-run analysis.
    """
    def __init__(self, Time, Ticks, Priority, FlowID, ExecID, ConnectID, ObjID, Op, PcktIdx, Packet, MsgType, Rate):
        self.Time = Time  # time in float64
        self.Ticks = Ticks  # ticks variable of time
        self.Priority = Priority  # priority field of time-stamp
        self.FlowID = FlowID  # integer identifier identifying the chain of traces this is part of
        self.ExecID = ExecID
        self.ConnectID = ConnectID  # integer identifier of the network connection
        self.ObjID = ObjID  # integer id for object being referenced
        self.Op = Op  # "start", "stop", "enter", "exit"
        self.PcktIdx = PcktIdx  # packet index inside of a multi-packet message
        self.Packet = Packet  # true if the event marks the passage of a packet (rather than flow)
        self.MsgType = MsgType
        self.Rate = Rate  # rate associated with the connection
    
    def trace_type(self):
        return TraceRecordType.NetworkType
    
    def serialize(self):
        return yaml.dump(vars(self), sort_keys=False)

class SchedulerTrace:
    """
    SchedulerTrace saves information about scheduling events in the simulation.
    """
    def __init__(self, Time, ObjID, ExecID, Op, Cores, Inservice, Inbckgrnd, Waiting):
        self.Time = Time
        self.ObjID = ObjID
        self.ExecID = ExecID
        self.Op = Op
        self.Cores = Cores
        self.Inservice = Inservice
        self.Inbckgrnd = Inbckgrnd
        self.Waiting = Waiting
    
    def serialize(self):
        return yaml.dump(vars(self), sort_keys=False)

class IntrfcTrace:
    """
    IntrfcTrace saves information about interface events in the simulation.
    """
    def __init__(self, Time, ObjID, ExecID, MsgID, Op, CQ):
        self.Time = Time
        self.ObjID = ObjID
        self.ExecID = ExecID
        self.MsgID = MsgID
        self.Op = Op
        self.CQ = CQ
    
    def serialize(self):
        return yaml.dump(vars(self), sort_keys=False)

def add_intrfc_trace(tm: TraceManager, vrt: vrtime.Time, execID, msgID, objID, op, CQ):
    """
    AddIntrfcTrace creates a record of the trace using its calling arguments, and stores it
    """
    it = IntrfcTrace(Time=vrt.Seconds(), ObjID=objID, ExecID=execID, MsgID=msgID, Op=op, CQ=CQ)
    it_str = it.serialize()
    str_time = str(it.Time) # TODO: make sure this prints the same as Go version
    trc_inst = TraceInst(TraceTime=str_time, TraceType="interface", TraceStr=it_str)
    tm.add_trace(vrt, execID, trc_inst)

def add_scheduler_trace(tm: TraceManager, vrt: vrtime.Time, ts, execID, objID, op):
    """
    AddSchedulerTrace creates a record of the trace using its calling arguments, and stores it
    ts is expected to have .cores, .inservice, .numWaiting, .inBckgrnd attributes
    """
    endpt = EndptDevById[objID]
    if not endpt.EndptState.Trace:
        return
    
    st = SchedulerTrace(Time=vrt.Seconds(), ObjID=objID, ExecID=execID, Op=op,
                        Cores=ts.cores, Inservice=ts.inservice, Inbckgrnd=ts.inBckgrnd, Waiting=ts.numWaiting)
    st_str = st.serialize()

    trace_time = str(vrt.Seconds()) # TODO: make sure this prints the same as Go version
    trc_inst = TraceInst(TraceTime=trace_time, TraceType="scheduler", TraceStr=st_str)
    tm.add_trace(vrt, execID, trc_inst)

def add_net_trace(tm: TraceManager, vrt: vrtime.Time, nm, objID, op):
    """
    AddNetTrace creates a record of the trace using its calling arguments, and stores it
    nm is expected to have .ConnectID, .FlowID, .ExecID, .PcktIdx, .NetMsgType attributes
    """
    ntr = NetTrace(Time=vrt.Seconds(), Ticks=vrt.Ticks(), Priority=vrt.Pri(),
                   FlowID=nm.FlowID, ExecID=nm.ExecID, ConnectID=nm.ConnectID, ObjID=objID,
                   Op=op, PcktIdx=nm.PcktIdx, Packet=False,
                   MsgType=nmtToStr[nm.NetMsgType], Rate=0.0)
    
    ntr_str = ntr.serialize()
    trace_time = str(vrt.Seconds()) # TODO: make sure this prints the same as Go version
    
    trc_inst = TraceInst(TraceTime=trace_time, TraceType="network", TraceStr=ntr_str)
    tm.add_trace(vrt, ntr.ExecID, trc_inst)
