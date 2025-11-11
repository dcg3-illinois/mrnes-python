import functools
from typing import Dict, List, Callable, Any, Optional
import math
import os
from evt_python.evt import evtm
from evt_python.evt import vrtime
import rngstream # maybe??
import net
import scheduler
import trace
import desc_topo
import param
import flow


# opTimeDesc: describes operation timing for a device
class opTimeDesc:
    def __init__(self, execTime: float, bndwdth: float, pcktLen: int):
        self.execTime = execTime
        self.bndwdth = bndwdth
        self.pcktLen = pcktLen

# devExecTimeTbl: map[operation type][device model] -> list of opTimeDesc
devExecTimeTbl: Dict[str, Dict[str, List[opTimeDesc]]] = {}
# OpMethod: Callable type for device operation methods
OpMethod = Callable[[net.TopoDev, str, net.NetworkMsg], float]

# set up an empty method to test against when looking to follow the link to the OpMethod
def emptyOpMethod(topo: net.TopoDev, arg: str, msg: net.NetworkMsg) -> float:
    return 0.0

# first index is device name, second index is either SwitchOp or RouteOp from message
devExecOpTbl: Dict[str, Dict[str, OpMethod]] = {}

# QkNetSim is set from the command line, when selected uses 'quick' form of network simulation
QkNetSim: bool = False

DefaultInt: Dict[str, int] = {}
DefaultFloat: Dict[str, float] = {}
DefaultBool: Dict[str, bool] = {}
DefaultStr: Dict[str, str] = {}

# TaskSchedulerByHostName maps an identifier for the scheduler to the scheduler itself
TaskSchedulerByHostName: Dict[str, scheduler.TaskScheduler] = {}

# AccelSchedulersByHostName maps an identifier for the map of schedulers to the map
AccelSchedulersByHostName: Dict[str, Dict[str, scheduler.TaskScheduler]] = {}

u01List: List[float] = []
numU01: int = 10000

defaultIntrfcBndwdth: float = 100.0
DefaultRouteOp: str = "route"
DefaultSwitchOp: str = "switch"

def buildDevExecTimeTbl(detl) -> Dict[str, Dict[str, List[opTimeDesc]]]:
    """
    buildDevExecTimeTbl creates a map structure that stores information about
    operations on switches and routers.
    The organization is:
        map[operation type] -> map[device model] -> list of execution times (including pcktlen and bndwdth)
    """
    det: Dict[str, Dict[str, List[opTimeDesc]]] = {}
    
    # the device timings are organized in the desc structure as a map indexed by operation type
    # (e.g., "switch", "route")
    for opType, mapList in detl.Times.items():
        
        # initialize the value for map[opType] if needed
        if opType not in det:
            det[opType] = {}

        # loop over all the records in the desc list associated with the dev op, getting and including the
        # device 'model' identifier and the execution time
        bndwdthByModel = {}

        for devExecDesc in mapList:
            model = devExecDesc.Model
            bndwdth = devExecDesc.Bndwdth
            
            if model not in bndwdthByModel:
                bndwdthByModel[model] = bndwdth
            
            if abs(bndwdth - bndwdthByModel[model]) > 1e-3:
                raise Exception(f"conflicting bndwdths in devOp {opType} for model {model}")
            
            if not (bndwdth > 0.0):
                bndwdth = 1000.0
            
            if model not in det[opType]:
                det[opType][model] = []
            
            det[opType][model].append(opTimeDesc(execTime=devExecDesc.ExecTime, pcktLen=devExecDesc.PcktLen, bndwdth=bndwdth))
        
        # make sure that list is sorted by packet length
        for model in det[opType]:
            det[opType][model].sort(key=lambda x: x.pcktLen)

    # add default's for "switch" and "route"
    if "switch" not in det:
        det["switch"] = {}
    if "default" not in det["switch"]:
        det["switch"]["default"] = []
    if len(det["switch"]["default"]) == 0:
        det["switch"]["default"].append(opTimeDesc(execTime=20e-6, pcktLen=0, bndwdth=defaultIntrfcBndwdth))
    
    if "route" not in det:
        det["route"] = {}
    if "default" not in det["route"]:
        det["route"]["default"] = []
    if len(det["route"]["default"]) == 0:
        det["route"]["default"].append(opTimeDesc(execTime=100e-6, pcktLen=0, bndwdth=defaultIntrfcBndwdth))
    return det

devTraceMgr: trace.TraceManager = None  # type: Any

# infrastructure for inter-func addressing (including x-compPattern addressing)

class MrnesApp:
    # a globally unique name for the application
    def GlobalName(self) -> str:
        raise NotImplementedError
    
    # an event handler to call to present a message to an app
    def ArrivalFunc(self) -> evtm.EventHandlerFunction:
        raise NotImplementedError

# NullHandler exists to provide as a link for data fields that call for
# an event handler, but no event handler is actually needed
def NullHandler(evtMgr: evtm.EventManager, context: Any, msg: Any) -> Any:
    return None

# LoadTopo reads in a topology configuration file and creates from it internal data
# structures representing the topology.  idCounter starts the enumeration of unique
# topology object names, and traceMgr is needed to log the names and ids of all the topology objects into the trace dictionary
def LoadTopo(topoFile: str, idCounter: int, traceMgr: trace.TraceManager) -> Optional[Exception]:
    empty = bytes()
    ext = os.path.splitext(topoFile)[1]
    useYAML = ext in [".yaml", ".yml"]

    tc, err = desc_topo.ReadTopoCfg(topoFile, useYAML, empty)

    if err is not None:
        return err
    
    global NumIDs, devTraceMgr

    # populate topology data structures that enable reference to the structures just read in
    # initialize NumIDs for generation of unique device/network ids
    NumIDs = idCounter
    
    # put traceMgr in global variable for reference
    devTraceMgr = traceMgr
    createTopoReferences(tc, traceMgr)
    return None

# LoadDevExec reads in the device-oriented function timings, puts
# them in a global table devExecTimeTbl
def LoadDevExec(devExecFile: str) -> Optional[Exception]:
    empty = bytes()
    ext = os.path.splitext(devExecFile)[1]
    useYAML = ext in [".yaml", ".yml"]
    del_, err = desc_topo.ReadDevExecList(devExecFile, useYAML, empty)
    if err is not None:
        return err
    
    global devExecTimeTbl, devExecOpTbl
    devExecTimeTbl = buildDevExecTimeTbl(del_)
    devExecOpTbl = {}
    return None

# LoadStateParams takes the file names of a 'base' file of performance
# parameters (e.g., defaults) and a 'modify' file of performance parameters
# to merge in (e.g. with higher specificity) and initializes the topology
# elements state structures with these.
def LoadStateParams(base: str) -> Optional[Exception]:
    empty = bytes()
    ext = os.path.splitext(base)[1]
    useYAML = ext in [".yaml", ".yml"]

    xd, err = param.ReadExpCfg(base, useYAML, empty)
    if err is not None:
        return err
    
    # use configuration parameters to initialize topology state
    SetTopoState(xd)
    return None

def BuildExperimentNet(evtMgr: evtm.EventManager, dictFiles: Dict[str, str], useYAML: bool, idCounter: int, traceMgr: trace.TraceManager):
    """
    BuildExperimentNet bundles the functions of LoadTopo, LoadDevExec, and LoadStateParams
    """
    topoFile = dictFiles["topo"]
    devExecFile = dictFiles["devExec"]
    baseFile = dictFiles["exp"]

    flow.init_flow_list()

    # load device execution times first so that initialization of
    # topo interfaces get access to device timings
    err1 = LoadDevExec(devExecFile)
    err2 = LoadTopo(topoFile, idCounter, traceMgr)
    err3 = LoadStateParams(baseFile)

    bckgrndRNG = rngstream.New("bckgrnd")
    global u01List
    u01List = [bckgrndRNG.RandU01() for _ in range(numU01)]

    errs = [err1, err2, err3]

    flow.start_flows(evtMgr)

    # note that None is returned if all errors are None
    return desc_topo.ReportErrs(errs)

# helper function to compare sg list elements
def sg_compare(a, b):
    cmp = param.CompareAttrbs(a.Attributes, b.Attributes)
    if(cmp == -1):
        return True
    else:
        return False

# helper function to compare nm list elements
def nm_compare(a, b):
    cmp = param.CompareAttrbs(a.Attributes, b.Attributes)
    if(cmp == -1):
        return True
    elif(cmp == 1):
        return False

    if(a.Param < b.Param):
        return True
    elif(a.Param > b.Param):
        return False
    return a.Value < b.Value

def reorderExpParams(pL):
    """
    reorderExpParams is used to put the ExpParameter parameters in
    an order such that the earlier elements in the order have broader
    range of attributes than later ones that apply to the same configuration element.
    This is entirely the same idea as is the approach of choosing a routing rule that has the
    smallest subnet range, when multiple rules apply to the same IP address
    """
    # partition the list into three sublists: wildcard (wc), single (sg), and named (nm).
    # The wildcard elements always appear before any others, and the named elements always
    # appear after all the others.
    wc: List[param.ExpParameter] = []
    nm: List[param.ExpParameter] = []
    sg: List[param.ExpParameter] = []

    # assign wc, sg, or nm based on attribute
    for param in pL:
        assigned = False

        # each parameter assigned to one of three lists
        for attrb in param.Attributes:
            # wildcard list?
            if attrb.AttrbName == "*":
                wc.append(param)
                assigned = True
                break
            # name list?
            elif attrb.AttrbName == "name":
                nm.append(param)
                assigned = True
                break
        # all attributes checked and none whose names are '*' or 'name'
        if not assigned:
            sg.append(param)

    # we do further rearrangement to bring identical elements together for detection and cleanup.
    # The wild card entries are identical in the ParamObj and Attribute fields, so order them based on the parameter.
    wc.sort(key=lambda x: x.Param)

    # sort the sg elements by (Attribute, Param) key
    sg.sort(key=functools.cmp_to_key(sg_compare))

    # sort the named elements by the (Attribute, Param) key
    nm.sort(key=functools.cmp_to_key(nm_compare))

    # pull them together with wc first, followed by sg, and finally nm
    ordered = wc + sg + nm

    # get rid of duplicates
    idx = len(ordered) - 1
    while idx > 0:
        if ordered[idx].Eq(ordered[idx-1]):
            del ordered[idx]
        idx -= 1

    return ordered


def SetTopoState(expCfg: param.ExpCfg):
    """
    SetTopoState creates the state structures for the devices before initializing from configuration files
    """
    SetTopoParameters(expCfg)


def SetTopoParameters(expCfg):
    """
    SetTopoParameters takes the list of parameter configurations expressed in
    ExpCfg form, turns its elements into configuration commands that may
    initialize multiple objects, includes globally applicable assignments
    and assign these in greatest-to-least application order
    """
    # this call initializes some maps used below
    param.GetExpParamDesc()

    # defaultParamList will hold initial ExpParameter specifications for
    # all parameter types. Some of these will be overwritten by more
    # specified assignments
    defaultParamList = []
    for paramObj in param.ExpParamObjs:
        for param in param.ExpParams[paramObj]:
            vs = ""
            if param == "switch":
                vs = "10e-6"
            elif param == "latency":
                vs = "10e-3"
            elif param == "delay":
                vs = "10e-6"
            elif param == "bandwidth":
                vs = "10"
            elif param == "buffer":
                vs = "100"
            elif param == "capacity":
                vs = "10"
            elif param == "MTU":
                vs = "1560"
            elif param == "trace":
                vs = "false"
            
            if len(vs) > 0:
                wcAttrb = [param.AttrbStruct(AttrbName="*", AttrbValue="")]
                expParam = param.ExpParameter(ParamObj=paramObj, Attributes=wcAttrb, Param=param, Value=vs)
                defaultParamList.append(expParam)

    # separate the parameters into the ParamObj groups they apply to
    endptParams: List[param.ExpParameter] = []
    netParams: List[param.ExpParameter] = []
    rtrParams: List[param.ExpParameter] = []
    swtchParams: List[param.ExpParameter] = []
    intrfcParams: List[param.ExpParameter] = []
    flowParams: List[param.ExpParameter] = []

    for param in expCfg.Parameters:
        if param.ParamObj == "Endpoint":
            endptParams.append(param)
        elif param.ParamObj == "Router":
            rtrParams.append(param)
        elif param.ParamObj == "Switch":
            swtchParams.append(param)
        elif param.ParamObj == "Interface":
            intrfcParams.append(param)
        elif param.ParamObj == "Network":
            netParams.append(param)
        elif param.ParamObj == "Flow":
            flowParams.append(param)
        else:
            raise Exception("surprise ParamObj")

    # reorder each list to assure the application order of most-general-first, and remove duplicates
    endptParams = reorderExpParams(endptParams)
    rtrParams = reorderExpParams(rtrParams)
    swtchParams = reorderExpParams(swtchParams)
    intrfcParams = reorderExpParams(intrfcParams)
    netParams = reorderExpParams(netParams)
    flowParams = reorderExpParams(flowParams)

    # concatenate defaultParamList and these lists. Note that this places the defaults
    # we created above before any defaults read in from file, so that if there are conflicting
    # default assignments the one the user put in the startup file will be applied after the
    # default default we create in this program
    orderedParamList = defaultParamList + endptParams + rtrParams + swtchParams + intrfcParams + netParams + flowParams

    # get the names of all network objects, separated by their network object type
    switchList: List[net.paramObj] = list(SwitchDevByID.values())
    routerList: List[net.paramObj] = list(RouterDevByID.values())
    endptList: List[net.paramObj] = list(EndptDevByID.values())
    netList: List[net.paramObj] = list(NetworkByID.values())
    flowList: List[net.paramObj] = list(FlowByID.values())
    intrfcList: List[net.paramObj] = list(IntrfcByID.values())

    # go through the sorted list of parameter assignments, more general before more specific
    for param in orderedParamList:
        # create a list that limits the objects to test to those that have required type
        if param.ParamObj == "Switch":
            testList = switchList
        elif param.ParamObj == "Router":
            testList = routerList
        elif param.ParamObj == "Endpoint":
            testList = endptList
        elif param.ParamObj == "Interface":
            testList = intrfcList
        elif param.ParamObj == "Network":
            testList = netList
        elif param.ParamObj == "Flow":
            testList = flowList
        else:
            continue

        testObjType = param.ParamObj

        # for every object in the constrained list test whether the attributes match.
        # Observe that
        #	 - * denotes a wild card
        #   - a set of attributes all of which need to be matched by the object
        #     is expressed as a comma-separated list
        #   - If a name "Fred" is given as an attribute, what is specified is "name%%Fred"
        for testObj in testList:
            # separate out the items in a comma-separated list
            
            matched = True
            for attrb in param.Attributes:
                attrbName = attrb.AttrbName
                attrbValue = attrb.AttrbValue

                # wild card means set.  Should be the case that if '*' is present
                # there is nothing else, but '*' overrides all
                if attrbName == "*":
                    matched = True
                    defaultName = testObjType + "-" + attrbName
                    defaultValue, defaultTypes = stringToValueStruct(attrbValue)
                    for vtype in defaultTypes:
                        if vtype == "int":
                            DefaultInt[defaultName] = defaultValue.intValue
                        elif vtype == "float":
                            DefaultFloat[defaultName] = defaultValue.floatValue
                        elif vtype == "bool":
                            DefaultBool[defaultName] = defaultValue.boolValue
                        elif vtype == "string":
                            DefaultStr[defaultName] = defaultValue.stringValue
                    break

                # if any of the attributes don't match we don't match
                if not testObj.matchParam(attrbName, attrbValue):
                    matched = False
                    break
            
            # this object passed the match test so apply the parameter value
            if matched:
                # the parameter value might be a string, or float, or bool.
                # stringToValue figures it out and returns value assignment in vs
                vs, _ = stringToValueStruct(param.Value)
                testObj.setParam(param.Param, vs)

# TODO: I think this isn't correct because floats would be misidentified as ints
def stringToValueStruct(v):
    """
    stringToValueStruct takes a string (used in the run-time configuration phase)
    and determines whether it is an integer, floating point, or a string
    """
    vs = param.valueStruct(intValue=0, floatValue=0.0, stringValue="", boolValue=False)
    try:
        # try conversion to int
        ivalue = int(v)
        vs.intValue = ivalue
        if ivalue == 1:
            vs.boolValue = True
        vs.floatValue = float(ivalue)
        return vs, ["int", "float"]
    except Exception:
        pass

    try:
        # try conversion to float
        fvalue = float(v)
        vs.floatValue = fvalue
        return vs, ["float"]
    except Exception:
        pass

    # left with it being a string. Check if it's true
    if v == "true" or v == "True":
        vs.boolValue = True
        return vs, ["bool"]
    
    vs.stringValue = v
    return vs, ["string"]

# global variables for finding things given an id, or a name
paramObjByID: Dict[int, net.paramObj] = {}
paramObjByName: Dict[str, net.paramObj] = {}

RouterDevByID: Dict[int, net.routerDev] = {}
RouterDevByName: Dict[str, net.routerDev] = {}

EndptDevByID: Dict[int, net.endptDev] = {}
EndptDevByName: Dict[str, net.endptDev] = {}

SwitchDevByID: Dict[int, net.switchDev] = {}
SwitchDevByName: Dict[str, net.switchDev] = {}

FlowByID: Dict[int, flow.Flow] = {}
FlowByName: Dict[str, flow.Flow] = {}

NetworkByID: Dict[int, net.networkStruct] = {}
NetworkByName: Dict[str, net.networkStruct] = {}

IntrfcByID: Dict[int, net.intrfcStruct] = {}
IntrfcByName: Dict[str, net.intrfcStruct] = {}

TopoDevByID: Dict[int, net.TopoDev] = {}
TopoDevByName: Dict[str, net.TopoDev] = {}

topoGraph: Dict[int, List[int]] = {}

# indices of directedTopoGraph are ids derived from position in directedNodes slide
directedTopoGraph: Dict[int, List[int]] = {}

# index of the directed node is its identity.  The 'i' component of the intPair is its devID, j is 0 if the source, 1 if dest
directedNodes: List[net.intPair] = []

# index is devID, i component of intPair is directed id of source, j is directed id of destination
devIDToDirected: Dict[int, List[net.intPair]] = {}

directedIDToDev: Dict[int, int] = {}

NumIDs = 0

def nxtID():
    """
    nxtID creates an id for objects created within mrnes module that are unique among those objects
    """
    global NumIDs
    NumIDs += 1
    return NumIDs


def GetExperimentNetDicts(dictFiles):
    """
    GetExperimentNetDicts accepts a map that holds the names of the input files used for the network part of an experiment,
    creates internal representations of the information they hold, and returns those structs.
    """
    tc: desc_topo.TopoCfg = None
    del_: desc_topo.DevExecList = None
    xd: param.ExpCfg = None
    xdx: param.ExpCfg = None

    empty = bytes()

    errs = []
    
    useYAML = False
    
    ext = os.path.splitext(dictFiles["topo"])[1]
    useYAML = ext in [".yaml", ".yml"]

    tc, err = desc_topo.ReadTopoCfg(dictFiles["topo"], useYAML, empty)
    errs.append(err)

    ext = os.path.splitext(dictFiles["devExec"])[1]
    useYAML = ext in [".yaml", ".yml"]

    del_, err = desc_topo.ReadDevExecList(dictFiles["devExec"], useYAML, empty)
    errs.append(err)

    ext = os.path.splitext(dictFiles["exp"])[1]
    useYAML = ext in [".yaml", ".yml"]

    xd, err = param.ReadExpCfg(dictFiles["exp"], useYAML, empty)
    errs.append(err)

    err = desc_topo.ReportErrs(errs)
    if err is not None:
        raise Exception(err)

    # ensure that the configuration parameter lists are built
    param.GetExpParamDesc()

    return tc, del_, xd, xdx


def connectDirectedIds(dtg: Dict[int, List[int]], id1: int, id2: int):
    # shouldn't happen
    if id1 == id2:
        return
    
    # create edge from source version of id1 to destination version of id2
    srcDevID = devIDToDirected[id1].i
    dstDevID = devIDToDirected[id2].j
    dtg.setdefault(srcDevID, []).append(dstDevID)

# connectIds remembers the asserted communication linkage between
# devices with given id numbers through modification of the input map tg
def connectIds(tg: Dict[int, List[int]], id1: int, id2: int, intrfc1: int, intrfc2: int):
    global routeStepIntrfcs
    if 'routeStepIntrfcs' not in globals() or routeStepIntrfcs is None:
        routeStepIntrfcs = {}

    # don't save connections to self if offered
    if id1 == id2:
        return
    
    # add id2 to id1's list of peers, if not already present
    if id2 not in tg.get(id1, []):
        tg.setdefault(id1, []).append(id2)

    # add id1 to id2's list of peers, if not already present
    if id1 not in tg.get(id2, []):
        tg.setdefault(id2, []).append(id1)
    routeStepIntrfcs[net.intPair(i=id1, j=id2)] = net.intPair(i=intrfc1, j=intrfc2)

def createTopoReferences(topoCfg, tm):
    """
    createTopoReferences reads from the input TopoCfg file to create references
    """
    global TopoDevByID, TopoDevByName, paramObjByID, paramObjByName
    global EndptDevByID, EndptDevByName, SwitchDevByID, SwitchDevByName
    global RouterDevByID, RouterDevByName, NetworkByID, NetworkByName
    global FlowByID, FlowByName, IntrfcByID, IntrfcByName
    global topoGraph, directedTopoGraph, directedNodes, devIDToDirected, directedIDToDev

    # initialize the maps and slices used for object lookup
    TopoDevByID = {}
    TopoDevByName = {}
    
    paramObjByID = {}
    paramObjByName = {}
    
    EndptDevByID = {}
    EndptDevByName = {}
    
    SwitchDevByID = {}
    SwitchDevByName = {}
    
    RouterDevByID = {}
    RouterDevByName = {}
    
    NetworkByID = {}
    NetworkByName = {}
    
    FlowByID = {}
    FlowByName = {}
    
    IntrfcByID = {}
    IntrfcByName = {}
    
    topoGraph = {}
    directedTopoGraph = {}
    directedNodes = []
    devIDToDirected = {}
    directedIDToDev = {}

    # fetch the router descriptions
    for rtr in topoCfg.Routers:
        # create a runtime representation from its desc representation
        rtrDev = net.createRouterDev(rtr)
        
        # get name and id
        rtrName = rtrDev.RouterName
        rtrID = rtrDev.RouterID
        
        # add rtrDev to TopoDev map
        # save rtrDev for lookup by Id and Name
        # for TopoDev interface
        addTopoDevLookup(rtrID, rtrName, rtrDev)
        RouterDevByID[rtrID] = rtrDev
        RouterDevByName[rtrName] = rtrDev
        
        # for paramObj interface
        paramObjByID[rtrID] = rtrDev
        paramObjByName[rtrName] = rtrDev

        # store id -> name for trace
        tm.AddName(rtrID, rtrName, "router")

    # fetch the switch descriptions
    for swtch in topoCfg.Switches:
        # create a runtime representation from its desc representation
        switchDev = net.switchDev.create_switch_dev(swtch)

        # get name and id
        switchName = switchDev.SwitchName
        switchID = switchDev.SwitchID

        # save switchDev for lookup by Id and Name
        # for TopoDev interface
        addTopoDevLookup(switchID, switchName, switchDev)
        SwitchDevByID[switchID] = switchDev
        SwitchDevByName[switchName] = switchDev

        # for paramObj interface
        paramObjByID[switchID] = switchDev
        paramObjByName[switchName] = switchDev

        # store id -> name for trace
        tm.AddName(switchID, switchName, "switch")

    # fetch the endpt descriptions
    for endpt in topoCfg.Endpts:
        # create a runtime representation from its desc representation
        endptDev = net.endptDev.create_endpt_dev(endpt)
        endptDev.initTaskScheduler()

        # get name and id
        endptName = endptDev.EndptName
        endptID = endptDev.EndptID
        
        # save endptDev for lookup by Id and Name
        # for TopoDev interface
        addTopoDevLookup(endptID, endptName, endptDev)
        EndptDevByID[endptID] = endptDev
        EndptDevByName[endptName] = endptDev
        
        # for paramObj interface
        paramObjByID[endptID] = endptDev
        paramObjByName[endptName] = endptDev
        
        # store id -> name for trace
        tm.AddName(endptID, endptName, "endpt")

    # fetch the network descriptions
    for netDesc in topoCfg.Networks:
        # create a runtime representation from its desc representation
        netobj = net.networkStruct.create_network_struct(netDesc)
        
        # save pointer to net accessible by id or name
        NetworkByID[netobj.Number] = netobj
        NetworkByName[netobj.Name] = netobj
        
        # for paramObj interface
        paramObjByID[netobj.Number] = netobj
        paramObjByName[netobj.Name] = netobj
        
        # store id -> name for trace
        tm.AddName(netobj.Number, netobj.Name, "network")

    # include lists of interfaces for each device
    for rtrDesc in topoCfg.Routers:
        for intrfc in rtrDesc.Interfaces:

            # create a runtime representation from its desc representation
            is_ = net.create_intrfc_struct(intrfc)
            
            # save is for reference by id or name
            IntrfcByID[is_.Number] = is_
            IntrfcByName[intrfc.Name] = is_
            
            # for paramObj interface
            paramObjByID[is_.Number] = is_
            paramObjByName[intrfc.Name] = is_
            
            # store id -> name for trace
            tm.AddName(is_.Number, intrfc.Name, "interface")
            
            rtr = RouterDevByName[rtrDesc.Name]
            rtr.addIntrfc(is_)

    # endpoint interfaces
    for endptDesc in topoCfg.Endpts:
        for intrfc in endptDesc.Interfaces:
            # create a runtime representation from its desc representation
            is_ = net.create_intrfc_struct(intrfc)
            
            # save is for reference by id or name
            IntrfcByID[is_.Number] = is_
            IntrfcByName[intrfc.Name] = is_
            
            # store id -> name for trace
            tm.AddName(is_.Number, intrfc.Name, "interface")
            
            # for paramObj interface
            paramObjByID[is_.Number] = is_
            paramObjByName[intrfc.Name] = is_
            
            # look up endpoint, use not from endpoint's desc representation
            endpt = EndptDevByName[endptDesc.Name]
            endpt.addIntrfc(is_)

    # switch interfaces
    for switchDesc in topoCfg.Switches:
        for intrfc in switchDesc.Interfaces:
            # create a runtime representation from its desc representation
            is_ = net.create_intrfc_struct(intrfc)
            
            # save is for reference by id or name
            IntrfcByID[is_.Number] = is_
            IntrfcByName[intrfc.Name] = is_
            
            # store id -> name for trace
            tm.AddName(is_.Number, intrfc.Name, "interface")
            
            # for paramObj interface
            paramObjByID[is_.Number] = is_
            paramObjByName[intrfc.Name] = is_
            
            # look up switch, using switch name from desc representation
            swtch = SwitchDevByName[switchDesc.Name]
            swtch.addIntrfc(is_)

    # fetch the flow descriptions
    for flowDesc in topoCfg.Flows:
        # create a runtime representation from its desc representation
        bgf = createBgfStruct(flowDesc)
        
        # nil returned if parameters don't support a flow (like zero rate)
        if bgf is None:
            continue
        
        # save pointer to net accessible by id or name
        FlowByID[bgf.FlowID] = bgf
        FlowByName[bgf.Name] = bgf
        
        # for paramObj interface
        paramObjByID[bgf.Number] = bgf
        paramObjByName[bgf.Name] = bgf
        
        # store id -> name for trace
        tm.AddName(bgf.Number, bgf.Name, "flow")

    # link the connect fields, now that all interfaces are known
    # loop over routers
    for rtrDesc in topoCfg.Routers:
        # loop over interfaces the router endpoints
        for intrfc in rtrDesc.Interfaces:
            # link the run-time representation of this interface to the
            # run-time representation of the interface it connects, if any
            # set the run-time pointer to the network faced by the interface
            net.linkIntrfcStruct(intrfc)
    
    # loop over endpoints
    for endptDesc in topoCfg.Endpts:
        # loop over interfaces the endpoint endpoints
        for intrfc in endptDesc.Interfaces:
            # link the run-time representation of this interface to the
            # run-time representation of the interface it connects, if any
            # set the run-time pointer to the network faced by the interface
            net.linkIntrfcStruct(intrfc)
    
    # loop over switches
    for switchDesc in topoCfg.Switches:
        # loop over interfaces the switch endpoints
        for intrfc in switchDesc.Interfaces:
            # link the run-time representation of this interface to the
            # run-time representation of the interface it connects, if any
            # set the run-time pointer to the network faced by the interface
            net.linkIntrfcStruct(intrfc)

    # networks have slices with pointers with things that
    # we know now are initialized, so can finish the initialization
    
    # loop over networks
    for netd in topoCfg.Networks:
        # find the run-time representation of the network
        netobj = NetworkByName[netd.Name]
        
        # initialize it from the desc description of the network
        netobj.initNetworkStruct(netd)

    for dev in TopoDevByID.values():
        devID = dev.DevID()
        
        # indices of directedTopoGraph are ids derived from position in directedNodes slide
        # index of the directed node is its identity.  The 'i' component of the intPair is its devID, j is 0 if the source, 1 if dest
        # index is devID, i component of intPair is directed id of source, j is directed id of destination
        
        # create directed nodes
        srcNode = net.intPair(i=devID, j=0)
        dstNode = net.intPair(i=devID, j=1)

        # obtain ids and remember them as mapped to by devID
        srcNodeID = len(directedNodes)
        directedNodes.append(srcNode)
        directedTopoGraph[srcNodeID] = []
        
        dstNodeID = len(directedNodes)
        directedNodes.append(dstNode)
        directedTopoGraph[dstNodeID] = []
        devIDToDirected[devID] = net.intPair(i=srcNodeID, j=dstNodeID)
        
        directedIDToDev[srcNodeID] = devID
        directedIDToDev[dstNodeID] = devID
        
        # if the device is not an endpoint create an edge from destination node to source node
        if dev.DevType() is not net.EndptCode:
            directedTopoGraph[dstNodeID].append(srcNodeID)

    # put all the connections recorded in the Cabled and Wireless fields into the topoGraph
    for dev in TopoDevByID.values():
        devID = dev.DevID()
        for intrfc in dev.DevIntrfcs():
            connected = False
            # cabled connection
            if getattr(intrfc, 'Cable', None) is not None and getattr(intrfc.Cable, 'Device', None) is not None and compatibleIntrfcs(intrfc, intrfc.Cable):
                peerID = intrfc.Cable.Device.DevID()
                connectIds(topoGraph, devID, peerID, intrfc.Number, intrfc.Cable.Number)
                connectDirectedIds(directedTopoGraph, devID, peerID)
                connected = True
            # carried connection
            if not connected and hasattr(intrfc, 'Carry') and len(intrfc.Carry) > 0:
                for cintrfc in intrfc.Carry:
                    if compatibleIntrfcs(intrfc, cintrfc):
                        peerID = cintrfc.Device.DevID()
                        connectIds(topoGraph, devID, peerID, intrfc.Number, cintrfc.Number)
                        connectDirectedIds(directedTopoGraph, devID, peerID)
                        connected = True
            # wireless connection
            if not connected and hasattr(intrfc, 'Wireless') and len(intrfc.Wireless) > 0:
                for conn in intrfc.Wireless:
                    peerID = conn.Device.DevID()
                    connectIds(topoGraph, devID, peerID, intrfc.Number, conn.Number)
                    connectDirectedIds(directedTopoGraph, devID, peerID)


def createBgfStruct(fd):
    """
    createBgfStruct creates a Flow object from a FlowDesc
    """
    return flow.CreateFlow(fd.Name, fd.SrcDev, fd.DstDev, fd.ReqRate, fd.FrameSize, fd.Mode, 0, fd.Groups)


# compatibleIntrfcs checks whether the named pair of interfaces are compatible
# w.r.t. their state on cable, carry, and wireless
def compatibleIntrfcs(intrfc1: net.intrfcStruct, intrfc2: net.intrfcStruct) -> bool:
    if getattr(intrfc1, 'Cable', None) is not None and getattr(intrfc2, 'Cable', None) is not None:
        return True
    return hasattr(intrfc1, 'Carry') and len(intrfc1.Carry) > 0 and hasattr(intrfc2, 'Carry') and len(intrfc2.Carry) > 0

# addTopoDevLookup puts a new entry in the TopoDevByID and TopoDevByName
# maps if that entry does not already exist
def addTopoDevLookup(tdID: int, tdName: str, td: net.TopoDev):
    global TopoDevByID, TopoDevByName
    if tdID in TopoDevByID:
        raise Exception(f"index {tdID} over-used in TopoDevByID\n")
    
    if tdName in TopoDevByName:
        raise Exception(f"name {tdName} over-used in TopoDevByName\n")
    
    TopoDevByID[tdID] = td
    TopoDevByName[tdName] = td


def InitializeBckgrndEndpt(evtMgr, endptDev):
    global u01List, numU01
    if not (endptDev.EndptState.BckgrndRate > 0.0):
        return
    
    ts = endptDev.EndptSched
    
    # only do this once
    if getattr(ts, 'bckgrndOn', False):
        return
    
    rho = endptDev.EndptState.BckgrndRate * endptDev.EndptState.BckgrndSrv
    
    # compute the initial number of busy cores
    busy = int(round(ts.cores * rho))

    # schedule the background task arrival process
    u01 = u01List[endptDev.EndptState.BckgrndIdx]
    endptDev.EndptState.BckgrndIdx = (endptDev.EndptState.BckgrndIdx + 1) % numU01

    arrival = -math.log(1.0-u01) / endptDev.EndptState.BckgrndRate
    evtMgr.Schedule(endptDev, None, scheduler.addBckgrnd, vrtime.SecondsToTime(arrival))
    
    # set some cores busy
    for idx in range(busy):
        ts.inBckgrnd += 1
        u01 = u01List[endptDev.EndptState.BckgrndIdx]
        endptDev.EndptState.BckgrndIdx = (endptDev.EndptState.BckgrndIdx + 1) % numU01
        service = -endptDev.EndptState.BckgrndSrv * math.log(1.0-u01)
        evtMgr.Schedule(endptDev, None, scheduler.rmBckgrnd, vrtime.SecondsToTime(service))

    ts.bckgrndOn = True


def InitializeBckgrnd(evtMgr: evtm.EventManager):
    for endptDev in EndptDevByName.values():
        InitializeBckgrndEndpt(evtMgr, endptDev)

