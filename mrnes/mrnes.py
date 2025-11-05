from typing import Dict, List, Callable, Any, Optional
import math
import os
# Placeholders for actual modules: evtm, vrtime, rngstream, etc.
from evt_python.evt import evtm
from evt_python.evt import vrtime
import rngstream # maybe??
import net


# opTimeDesc: describes operation timing for a device
class opTimeDesc:
    def __init__(self, execTime: float, bndwdth: float, pcktLen: int):
        self.execTime = execTime
        self.bndwdth = bndwdth
        self.pcktLen = pcktLen

# devExecTimeTbl: map[operation type][device model] -> list of opTimeDesc
devExecTimeTbl: Dict[str, Dict[str, List[opTimeDesc]]] = {}
# OpMethod: Callable type for device operation methods
# OpMethod = Callable[[TopoDev, str, NetworkMsg], float]  # Uncomment and define TopoDev/NetworkMsg as needed

def emptyOpMethod(topo: net.TopoDev, arg: str, msg: net.NetworkMsg) -> float:
    # topo: TopoDev, arg: str, msg: NetworkMsg
    return 0.0

# first index is device name, second index is either SwitchOp or RouteOp from message
devExecOpTbl: Dict[str, Dict[str, Callable]] = {}

# QkNetSim is set from the command line, when selected uses 'quick' form of network simulation
QkNetSim: bool = False

DefaultInt: Dict[str, int] = {}
DefaultFloat: Dict[str, float] = {}
DefaultBool: Dict[str, bool] = {}
DefaultStr: Dict[str, str] = {}

# TaskSchedulerByHostName maps an identifier for the scheduler to the scheduler itself
TaskSchedulerByHostName: Dict[str, TaskScheduler] = {} # from scheduler.py

# AccelSchedulersByHostName maps an identifier for the map of schedulers to the map
AccelSchedulersByHostName: Dict[str, Dict[str, TaskScheduler]] = {}

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
    for opType, mapList in detl.Times.items():
        if opType not in det:
            det[opType] = {}
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

devTraceMgr = None  # type: Any

# MrnesApp interface (Python: ABC)
class MrnesApp:
    def GlobalName(self) -> str:
        raise NotImplementedError
    def ArrivalFunc(self):
        raise NotImplementedError

def NullHandler(evtMgr, context, msg):
    return None

def LoadTopo(topoFile: str, idCounter: int, traceMgr) -> Optional[Exception]:
    empty = bytes()
    ext = os.path.splitext(topoFile)[1]
    useYAML = ext in [".yaml", ".yml"]
    tc, err = ReadTopoCfg(topoFile, useYAML, empty)
    if err is not None:
        return err
    global NumIDs, devTraceMgr
    NumIDs = idCounter
    devTraceMgr = traceMgr
    createTopoReferences(tc, traceMgr)
    return None

def LoadDevExec(devExecFile: str) -> Optional[Exception]:
    empty = bytes()
    ext = os.path.splitext(devExecFile)[1]
    useYAML = ext in [".yaml", ".yml"]
    del_, err = ReadDevExecList(devExecFile, useYAML, empty)
    if err is not None:
        return err
    global devExecTimeTbl, devExecOpTbl
    devExecTimeTbl = buildDevExecTimeTbl(del_)
    devExecOpTbl = {}
    return None

def LoadStateParams(base: str) -> Optional[Exception]:
    empty = bytes()
    ext = os.path.splitext(base)[1]
    useYAML = ext in [".yaml", ".yml"]
    xd, err = ReadExpCfg(base, useYAML, empty)
    if err is not None:
        return err
    SetTopoState(xd)
    return None
