import functools
from typing import Dict, List, Callable, Any, Optional, Tuple
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


# OpTimeDesc: describes operation timing for a device
class OpTimeDesc:
    def __init__(self, exec_time: float, bndwdth: float, pckt_len: int):
        self.exec_time = exec_time
        self.bndwdth = bndwdth
        self.pckt_len = pckt_len

# dev_exec_time_tbl: map[operation type][device model] -> list of OpTimeDesc
dev_exec_time_tbl: Dict[str, Dict[str, List[OpTimeDesc]]] = {}
# OpMethod: Callable type for device operation methods
OpMethod = Callable[[net.TopoDev, str, net.NetworkMsg], float]

# set up an empty method to test against when looking to follow the link 
# to the OpMethod
def empty_op_method(topo: net.TopoDev, arg: str, msg: net.NetworkMsg) -> float:
    return 0.0

# first index is device name, second index is either SwitchOp or 
# RouteOp from message
dev_exec_op_tbl: Dict[str, Dict[str, OpMethod]] = {}

# qk_net_sim is set from the command line, when selected uses 'quick' 
# form of network simulation
qk_net_sim: bool = False

default_int: Dict[str, int] = {}
default_float: Dict[str, float] = {}
default_bool: Dict[str, bool] = {}
default_str: Dict[str, str] = {}

# maps an identifier for the scheduler to the scheduler itself
task_scheduler_by_host_name: Dict[str, scheduler.TaskScheduler] = {}

# maps an identifier for the map of schedulers to the map
accel_schedulers_by_host_name: Dict[str, Dict[str, scheduler.TaskScheduler]] = {}

u01_list: List[float] = []
num_u01: int = 10000

default_intrfc_bndwdth: float = 100.0
default_route_op: str = "route"
default_switch_op: str = "switch"

def build_dev_exec_time_tbl(detl) -> Dict[str, Dict[str, List[OpTimeDesc]]]:
    """
    build_dev_exec_time_tbl creates a map structure that stores information about
    operations on switches and routers.
    The organization is:
        map[operation type] -> map[device model] -> list of execution times 
        (including pcktlen and bndwdth)
    """
    det: Dict[str, Dict[str, List[OpTimeDesc]]] = {}
    
    # the device timings are organized in the desc structure as a map indexed 
    # by operation type (e.g., "switch", "route")
    for op_type, map_list in detl.Times.items():
        
        # initialize the value for map[op_type] if needed
        if op_type not in det:
            det[op_type] = {}

        # loop over all the records in the desc list associated with the dev op,
        # getting and including the device 'model' identifier and the execution
        # time
        bndwdth_by_model = {}

        for dev_exec_desc in map_list:
            model = dev_exec_desc.Model
            bndwdth = dev_exec_desc.Bndwdth
            
            if model not in bndwdth_by_model:
                bndwdth_by_model[model] = bndwdth
            
            if abs(bndwdth - bndwdth_by_model[model]) > 1e-3:
                raise Exception(f"conflicting bndwdths in devOp {op_type} for model {model}")
            
            if not (bndwdth > 0.0):
                bndwdth = 1000.0
            
            if model not in det[op_type]:
                det[op_type][model] = []
            
            det[op_type][model].append(OpTimeDesc(
                                    exec_time=dev_exec_desc.ExecTime, 
                                    pckt_len=dev_exec_desc.PcktLen, 
                                    bndwdth=bndwdth
                                    )
                                )
        
        # make sure that list is sorted by packet length
        for model in det[op_type]:
            det[op_type][model].sort(key=lambda x: x.pckt_len)

    # add default's for "switch" and "route"
    if "switch" not in det:
        det["switch"] = {}
    if "default" not in det["switch"]:
        det["switch"]["default"] = []
    if len(det["switch"]["default"]) == 0:
        det["switch"]["default"].append(OpTimeDesc(exec_time=20e-6, 
                                                   pckt_len=0, 
                                                   bndwdth=default_intrfc_bndwdth
                                                   ))
    
    if "route" not in det:
        det["route"] = {}
    if "default" not in det["route"]:
        det["route"]["default"] = []
    if len(det["route"]["default"]) == 0:
        det["route"]["default"].append(OpTimeDesc(exec_time=100e-6, 
                                                  pckt_len=0, 
                                                  bndwdth=default_intrfc_bndwdth
                                                    ))
    return det

dev_trace_mgr: trace.TraceManager = None  # type: Any

# infrastructure for inter-func addressing (including x-compPattern addressing)

class MrnesApp:
    # a globally unique name for the application
    def global_name(self) -> str:
        raise NotImplementedError
    
    # an event handler to call to present a message to an app
    def arrival_func(self) -> evtm.EventHandlerFunction:
        raise NotImplementedError

# null_handler exists to provide as a link for data fields that call for
# an event handler, but no event handler is actually needed
def null_handler(evt_mgr: evtm.EventManager, context: Any, msg: Any) -> Any:
    return None

# load_topo reads in a topology configuration file and creates from it internal 
# data structures representing the topology.  id_counter starts the enumeration 
# of unique topology object names, and trace_mgr is needed to log the names and 
# ids of all the topology objects into the trace dictionary
def load_topo(topo_file: str, id_counter: int, 
              trace_mgr: trace.TraceManager) -> Optional[Exception]:
    global num_ids, dev_trace_mgr

    empty = bytes()
    ext = os.path.splitext(topo_file)[1]
    use_yaml = ext in [".yaml", ".yml"]

    tc, err = desc_topo.read_topo_config(topo_file, use_yaml, empty)

    if err is not None:
        return err

    # populate topology data structures that enable reference to the structures 
    # just read in initialize num_ids for generation of unique device/network ids
    num_ids = id_counter
    
    # put trace_mgr in global variable for reference
    dev_trace_mgr = trace_mgr
    create_topo_references(tc, trace_mgr)
    return None

# load_dev_exec reads in the device-oriented function timings, puts
# them in a global table dev_exec_time_tbl
def load_dev_exec(dev_exec_file: str) -> Optional[Exception]:
    global dev_exec_time_tbl, dev_exec_op_tbl
    
    empty = bytes()
    ext = os.path.splitext(dev_exec_file)[1]
    use_yaml = ext in [".yaml", ".yml"]
    del_, err = desc_topo.read_dev_exec_list(dev_exec_file, use_yaml, empty)
    if err is not None:
        return err
    
    dev_exec_time_tbl = build_dev_exec_time_tbl(del_)
    dev_exec_op_tbl = {}
    return None

# load_state_params takes the file names of a 'base' file of performance
# parameters (e.g., defaults) and a 'modify' file of performance parameters
# to merge in (e.g. with higher specificity) and initializes the topology
# elements state structures with these.
def load_state_params(base: str) -> Optional[Exception]:
    empty = bytes()
    ext = os.path.splitext(base)[1]
    use_yaml = ext in [".yaml", ".yml"]

    xd, err = param.read_exp_cfg(base, use_yaml, empty)
    if err is not None:
        return err
    
    # use configuration parameters to initialize topology state
    set_topo_state(xd)
    return None

def build_experiment_net(evt_mgr: evtm.EventManager, dict_files: Dict[str, str],
                        use_yaml: bool, id_counter: int, 
                        trace_mgr: trace.TraceManager):
    """
    build_experiment_net bundles the functions of load_topo, load_dev_exec, and 
    load_state_params
    """
    global u01_list
    
    topo_file = dict_files["topo"]
    dev_exec_file = dict_files["devExec"]
    base_file = dict_files["exp"]

    flow.init_flow_list()

    # load device execution times first so that initialization of
    # topo interfaces get access to device timings
    err1 = load_dev_exec(dev_exec_file)
    err2 = load_topo(topo_file, id_counter, trace_mgr)
    err3 = load_state_params(base_file)

    bckgrnd_rng = rngstream.New("bckgrnd")
    
    u01_list = [bckgrnd_rng.RandU01() for _ in range(num_u01)]

    errs = [err1, err2, err3]

    flow.start_flows(evt_mgr)

    # note that None is returned if all errors are None
    return desc_topo.report_errs(errs)

# helper function to compare sg list elements
def sg_compare(a, b):
    cmp = param.compare_attrbs(a.attributes, b.attributes)
    if cmp == -1:
        return True
    else:
        return False

# helper function to compare nm list elements
def nm_compare(a, b):
    cmp = param.compare_attrbs(a.attributes, b.attributes)
    if(cmp == -1):
        return True
    elif(cmp == 1):
        return False

    if(a.param < b.param):
        return True
    elif(a.param > b.param):
        return False
    return a.value < b.value

def reorder_exp_params(param_list):
    """
    reorder_exp_params is used to put the ExpParameter parameters in
    an order such that the earlier elements in the order have broader
    range of attributes than later ones that apply to the same configuration
    element. This is entirely the same idea as is the approach of choosing a 
    routing rule that has the smallest subnet range, when multiple rules apply 
    to the same IP address
    """
    # partition the list into three sublists: wildcard (wc), single (sg), and 
    # named (nm). The wildcard elements always appear before any others, and
    # the named elements always appear after all the others.
    wc: List[param.ExpParameter] = []
    nm: List[param.ExpParameter] = []
    sg: List[param.ExpParameter] = []

    # assign wc, sg, or nm based on attribute
    for param in param_list:
        assigned = False

        # each parameter assigned to one of three lists
        for attrb in param.attributes:
            # wildcard list?
            if attrb.attrb_name == "*":
                wc.append(param)
                assigned = True
                break
            # name list?
            elif attrb.attrb_name == "name":
                nm.append(param)
                assigned = True
                break
        # all attributes checked and none whose names are '*' or 'name'
        if not assigned:
            sg.append(param)

    # we do further rearrangement to bring identical elements together for
    # detection and cleanup.
    # The wild card entries are identical in the ParamObj and Attribute fields, 
    # so order them based on the parameter.
    wc.sort(key=lambda x: x.param)

    # sort the sg elements by Attribute key
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


def set_topo_state(exp_cfg: param.ExpCfg):
    """
    set_topo_state creates the state structures for the devices before 
    initializing from configuration files
    """
    set_topo_parameters(exp_cfg)


def set_topo_parameters(exp_cfg):
    """
    set_topo_parameters takes the list of parameter configurations expressed in
    ExpCfg form, turns its elements into configuration commands that may
    initialize multiple objects, includes globally applicable assignments
    and assign these in greatest-to-least application order
    """
    # this call initializes some maps used below
    param.get_exp_param_desc()

    # default_param_list will hold initial ExpParameter specifications for
    # all parameter types. Some of these will be overwritten by more
    # specified assignments
    default_param_list = []
    # TODO: access of global variable from different file
    for param_obj in param.exp_param_objs:
        for p in param.exp_params[param_obj]:
            vs = ""
            if p == "switch":
                vs = "10e-6"
            elif p == "latency":
                vs = "10e-3"
            elif p == "delay":
                vs = "10e-6"
            elif p == "bandwidth":
                vs = "10"
            elif p == "buffer":
                vs = "100"
            elif p == "capacity":
                vs = "10"
            elif p == "MTU":
                vs = "1560"
            elif p == "trace":
                vs = "false"
            
            if len(vs) > 0:
                wc_attrb = [param.AttrbStruct(attrb_name="*", attrb_value="")]
                exp_param = param.ExpParameter(param_obj=param_obj, 
                                               attributes=wc_attrb, 
                                               param=p, 
                                               value=vs)
                default_param_list.append(exp_param)

    # separate the parameters into the ParamObj groups they apply to
    endpt_params: List[param.ExpParameter] = []
    net_params: List[param.ExpParameter] = []
    rtr_params: List[param.ExpParameter] = []
    swtch_params: List[param.ExpParameter] = []
    intrfc_params: List[param.ExpParameter] = []
    flow_params: List[param.ExpParameter] = []

    for p in exp_cfg.parameters:
        if p.param_obj == "Endpoint":
            endpt_params.append(p)
        elif p.param_obj == "Router":
            rtr_params.append(p)
        elif p.param_obj == "Switch":
            swtch_params.append(p)
        elif p.param_obj == "Interface":
            intrfc_params.append(p)
        elif p.param_obj == "Network":
            net_params.append(p)
        elif p.param_obj == "Flow":
            flow_params.append(p)
        else:
            raise Exception("surprise ParamObj")

    # reorder each list to assure the application order of most-general-first, 
    # and remove duplicates
    endpt_params = reorder_exp_params(endpt_params)
    rtr_params = reorder_exp_params(rtr_params)
    swtch_params = reorder_exp_params(swtch_params)
    intrfc_params = reorder_exp_params(intrfc_params)
    net_params = reorder_exp_params(net_params)
    flow_params = reorder_exp_params(flow_params)

    # concatenate default_param_list and these lists. Note that this places the 
    # defaults we created above before any defaults read in from file, so that 
    # if there are conflicting default assignments the one the user put in the 
    # startup file will be applied after the default default we create in this 
    # program
    ordered_param_list = default_param_list \
                         + endpt_params \
                         + rtr_params \
                         + swtch_params \
                         + intrfc_params \
                         + net_params \
                         + flow_params

    # get the names of all network objects, separated by their 
    # network object type
    switch_list: List[net.paramObj] = list(switch_dev_by_id.values())
    router_list: List[net.paramObj] = list(router_dev_by_id.values())
    endpt_list: List[net.paramObj] = list(endpt_dev_by_id.values())
    net_list: List[net.paramObj] = list(network_by_id.values())
    flow_list: List[net.paramObj] = list(flow_by_id.values())
    intrfc_list: List[net.paramObj] = list(intrfc_by_id.values())

    # go through the sorted list of parameter assignments, more general 
    # before more specific
    for p in ordered_param_list:
        # create a list that limits the objects to test to those that have 
        # required type
        if p.param_obj == "Switch":
            test_list = switch_list
        elif p.param_obj == "Router":
            test_list = router_list
        elif p.param_obj == "Endpoint":
            test_list = endpt_list
        elif p.param_obj == "Interface":
            test_list = intrfc_list
        elif p.param_obj == "Network":
            test_list = net_list
        elif p.param_obj == "Flow":
            test_list = flow_list
        else:
            continue

        test_obj_type = p.param_obj

        # for every object in the constrained list test whether the
        # attributes match. Observe that
        #   - * denotes a wild card
        #   - a set of attributes all of which need to be matched by the object
        #     is expressed as a comma-separated list
        #   - If a name "Fred" is given as an attribute, what is specified is 
        #     "name%%Fred"
        for test_obj in test_list:
            # separate out the items in a comma-separated list
            matched = True
            for attrb in p.attributes:
                attrb_name = attrb.attrb_name
                attrb_value = attrb.attrb_value

                # wild card means set.  Should be the case that if '*' is present
                # there is nothing else, but '*' overrides all
                if attrb_name == "*":
                    matched = True
                    default_name = test_obj_type + "-" + attrb_name
                    default_value, default_types = string_to_value_struct(
                                                                    attrb_value)
                    for vtype in default_types:
                        if vtype == "int":
                            default_int[default_name] = default_value.int_value
                        elif vtype == "float":
                            default_float[default_name] = default_value.float_value
                        elif vtype == "bool":
                            default_bool[default_name] = default_value.bool_value
                        elif vtype == "string":
                            default_str[default_name] = default_value.string_value
                    break

                # if any of the attributes don't match we don't match
                if not test_obj.match_param(attrb_name, attrb_value):
                    matched = False
                    break

            # this object passed the match test so apply the parameter value
            if matched:
                # the parameter value might be a string, or float, or bool.
                # string_to_value_struct figures it out and returns value
                # assignment in vs
                vs, _ = string_to_value_struct(p.value)
                test_obj.set_param(p.param, vs)

# TODO: I think this isn't correct because floats would be misidentified as ints
def string_to_value_struct(v) -> Tuple[param.valueStruct, List[str]]:
    """
    string_to_value_struct takes a string (used in the run-time 
    configuration phase) and determines whether it is an integer, 
    floating point, or a string
    """
    vs = param.valueStruct(int_value=0, 
                           float_value=0.0, 
                           string_value="", 
                           bool_value=False)
    # treat as integer only if it's a strict integer literal (no dots/exponent)
    s = v.strip()
    if s and ((s[0] in "+-" and s[1:].isdigit()) or s.isdigit()):
        ivalue = int(s)
        vs.int_value = ivalue
        if ivalue == 1:
            vs.bool_value = True
        vs.float_value = float(ivalue)
        return vs, ["int", "float"]

    try:
        # try conversion to float
        fvalue = float(v)
        vs.float_value = fvalue
        return vs, ["float"]
    except Exception:
        pass

    # left with it being a string. Check if it's true
    if v == "true" or v == "True":
        vs.bool_value = True
        return vs, ["bool"]

    vs.string_value = v
    return vs, ["string"]

# global variables for finding things given an id, or a name
param_obj_by_id: Dict[int, net.paramObj] = {}
param_obj_by_name: Dict[str, net.paramObj] = {}

router_dev_by_id: Dict[int, net.routerDev] = {}
router_dev_by_name: Dict[str, net.routerDev] = {}

endpt_dev_by_id: Dict[int, net.endptDev] = {}
endpt_dev_by_name: Dict[str, net.endptDev] = {}

switch_dev_by_id: Dict[int, net.switchDev] = {}
switch_dev_by_name: Dict[str, net.switchDev] = {}

flow_by_id: Dict[int, flow.Flow] = {}
flow_by_name: Dict[str, flow.Flow] = {}

network_by_id: Dict[int, net.networkStruct] = {}
network_by_name: Dict[str, net.networkStruct] = {}

intrfc_by_id: Dict[int, net.intrfcStruct] = {}
intrfc_by_name: Dict[str, net.intrfcStruct] = {}

topo_dev_by_id: Dict[int, net.TopoDev] = {}
topo_dev_by_name: Dict[str, net.TopoDev] = {}

topo_graph: Dict[int, List[int]] = {}

# indices of directed_topo_graph are ids derived from position in 
# directed_nodes slide
directed_topo_graph: Dict[int, List[int]] = {}

# index of the directed node is its identity.  The 'i' component of 
# the intPair is its devID, j is 0 if the source, 1 if dest
directed_nodes: List[net.intPair] = []

# index is devID, i component of intPair is directed id of source, 
# j is directed id of destination
dev_id_to_directed: Dict[int, List[net.intPair]] = {}

directed_id_to_dev: Dict[int, int] = {}

num_ids = 0

def nxt_id():
    """
    nxt_id creates an id for objects created within mrnes module 
    that are unique among those objects
    """
    global num_ids
    num_ids += 1
    return num_ids


def get_experiment_net_dicts(dict_files):
    """
    get_experiment_net_dicts accepts a map that holds the names of 
    the input files used for the network part of an experiment,
    creates internal representations of the information they hold, 
    and returns those structs.
    """
    tc: desc_topo.TopoCfg = None
    del_: desc_topo.DevExecList = None
    xd: param.ExpCfg = None
    xdx: param.ExpCfg = None

    empty = bytes()

    errs = []
    
    use_yaml = False
    
    ext = os.path.splitext(dict_files["topo"])[1]
    use_yaml = ext in [".yaml", ".yml"]

    tc, err = desc_topo.read_topo_cfg(dict_files["topo"], 
                                      use_yaml, 
                                      empty
                                    )
    errs.append(err)

    ext = os.path.splitext(dict_files["devExec"])[1]
    use_yaml = ext in [".yaml", ".yml"]

    del_, err = desc_topo.read_dev_exec_list(dict_files["devExec"], 
                                             use_yaml, 
                                             empty
                                            )
    errs.append(err)

    ext = os.path.splitext(dict_files["exp"])[1]
    use_yaml = ext in [".yaml", ".yml"]

    xd, err = param.read_exp_cfg(dict_files["exp"], use_yaml, empty)
    errs.append(err)

    err = desc_topo.report_errs(errs)
    if err is not None:
        raise Exception(err)

    # ensure that the configuration parameter lists are built
    param.get_exp_param_desc()

    return tc, del_, xd, xdx


def connect_directed_ids(dtg: Dict[int, List[int]], id1: int, id2: int):
    # shouldn't happen
    if id1 == id2:
        return
    
    # create edge from source version of id1 to destination version of id2
    src_dev_id = dev_id_to_directed[id1].i
    dst_dev_id = dev_id_to_directed[id2].j
    dtg.setdefault(src_dev_id, []).append(dst_dev_id)

# connect_ids remembers the asserted communication linkage between
# devices with given id numbers through modification of the input map tg
def connect_ids(tg: Dict[int, List[int]], 
                id1: int, id2: int, 
                intrfc1: int, intrfc2: int):
    
    global route_step_intrfcs
    if 'route_step_intrfcs' not in globals() or route_step_intrfcs is None:
        route_step_intrfcs = {}

    # don't save connections to self if offered
    if id1 == id2:
        return
    
    # add id2 to id1's list of peers, if not already present
    if id2 not in tg.get(id1, []):
        tg.setdefault(id1, []).append(id2)

    # add id1 to id2's list of peers, if not already present
    if id1 not in tg.get(id2, []):
        tg.setdefault(id2, []).append(id1)
    route_step_intrfcs[net.intPair(i=id1, j=id2)] = net.intPair(i=intrfc1, 
                                                                j=intrfc2)

# this jawn creates all of our dictionaries and lists
def create_topo_references(topo_cfg: desc_topo.TopoCfg, tm: trace.TraceManager):
    """
    create_topo_references reads from the input TopoCfg file to create references
    """
    global topo_dev_by_id, topo_dev_by_name, param_obj_by_id, param_obj_by_name
    global endpt_dev_by_id, endpt_dev_by_name, switch_dev_by_id, switch_dev_by_name
    global router_dev_by_id, router_dev_by_name, network_by_id, network_by_name
    global flow_by_id, flow_by_name, intrfc_by_id, intrfc_by_name
    global topo_graph, directed_topo_graph, directed_nodes
    global dev_id_to_directed, directed_id_to_dev

    # initialize the maps and slices used for object lookup
    topo_dev_by_id = {}
    topo_dev_by_name = {}
    
    param_obj_by_id = {}
    param_obj_by_name = {}
    
    endpt_dev_by_id = {}
    endpt_dev_by_name = {}
    
    switch_dev_by_id = {}
    switch_dev_by_name = {}
    
    router_dev_by_id = {}
    router_dev_by_name = {}
    
    network_by_id = {}
    network_by_name = {}
    
    flow_by_id = {}
    flow_by_name = {}
    
    intrfc_by_id = {}
    intrfc_by_name = {}
    
    topo_graph = {}
    directed_topo_graph = {}
    directed_nodes = []
    dev_id_to_directed = {}
    directed_id_to_dev = {}

    # fetch the router descriptions
    for rtr in topo_cfg.routers:
        # create a runtime representation from its desc representation
        rtr_dev = net.create_router_dev(rtr)
        
        # get name and id
        rtr_name = rtr_dev.router_name
        rtr_id = rtr_dev.router_id

        # add rtr_dev to TopoDev map
        # save rtr_dev for lookup by Id and Name
        # for TopoDev interface
        add_topo_dev_lookup(rtr_id, rtr_name, rtr_dev)
        router_dev_by_id[rtr_id] = rtr_dev
        router_dev_by_name[rtr_name] = rtr_dev
        
        # for paramObj interface
        param_obj_by_id[rtr_id] = rtr_dev
        param_obj_by_name[rtr_name] = rtr_dev

        # store id -> name for trace
        tm.add_name(rtr_id, rtr_name, "router")

    # fetch the switch descriptions
    for swtch in topo_cfg.switches:
        # create a runtime representation from its desc representation
        switch_dev = net.switchDev.create_switch_dev(swtch)

        # get name and id
        switch_name = switch_dev.switch_name
        switch_id = switch_dev.switch_id

        # save switch_dev for lookup by Id and Name
        # for TopoDev interface
        add_topo_dev_lookup(switch_id, switch_name, switch_dev)
        switch_dev_by_id[switch_id] = switch_dev
        switch_dev_by_name[switch_name] = switch_dev

        # for paramObj interface
        param_obj_by_id[switch_id] = switch_dev
        param_obj_by_name[switch_name] = switch_dev

        # store id -> name for trace
        tm.add_name(switch_id, switch_name, "switch")

    # fetch the endpt descriptions
    for endpt in topo_cfg.endpts:
        # create a runtime representation from its desc representation
        endpt_dev = net.endptDev.create_endpt_dev(endpt)
        endpt_dev.init_task_scheduler()

        # get name and id
        endpt_name = endpt_dev.endpt_name
        endpt_id = endpt_dev.endpt_id

        # save endpt_dev for lookup by Id and Name
        # for TopoDev interface
        add_topo_dev_lookup(endpt_id, endpt_name, endpt_dev)
        endpt_dev_by_id[endpt_id] = endpt_dev
        endpt_dev_by_name[endpt_name] = endpt_dev
        
        # for paramObj interface
        param_obj_by_id[endpt_id] = endpt_dev
        param_obj_by_name[endpt_name] = endpt_dev
        
        # store id -> name for trace
        tm.add_name(endpt_id, endpt_name, "endpt")

    # fetch the network descriptions
    for net_desc in topo_cfg.networks:
        # create a runtime representation from its desc representation
        netobj = net.networkStruct.create_network_struct(net_desc)
        
        # save pointer to net accessible by id or name
        network_by_id[netobj.number] = netobj
        network_by_name[netobj.name] = netobj

        # for paramObj interface
        param_obj_by_id[netobj.number] = netobj
        param_obj_by_name[netobj.name] = netobj
        
        # store id -> name for trace
        tm.add_name(netobj.number, netobj.name, "network")

    # include lists of interfaces for each device
    for rtr_desc in topo_cfg.routers:
        for intrfc in rtr_desc.interfaces:

            # create a runtime representation from its desc representation
            is_ = net.create_intrfc_struct(intrfc)
            
            # save is for reference by id or name
            intrfc_by_id[is_.number] = is_
            intrfc_by_name[intrfc.name] = is_

            # for paramObj interface
            param_obj_by_id[is_.number] = is_
            param_obj_by_name[intrfc.name] = is_
            
            # store id -> name for trace
            tm.add_name(is_.number, intrfc.name, "interface")

            rtr = router_dev_by_name[rtr_desc.name]
            rtr.add_intrfc(is_)

    # endpoint interfaces
    for endpt_desc in topo_cfg.endpts:
        for intrfc in endpt_desc.interfaces:
            # create a runtime representation from its desc representation
            is_ = net.create_intrfc_struct(intrfc)
            
            # save is for reference by id or name
            intrfc_by_id[is_.number] = is_
            intrfc_by_name[intrfc.name] = is_

            # store id -> name for trace
            tm.add_name(is_.number, intrfc.name, "interface")

            # for paramObj interface
            param_obj_by_id[is_.number] = is_
            param_obj_by_name[intrfc.name] = is_

            # look up endpoint, use not from endpoint's desc representation
            endpt = endpt_dev_by_name[endpt_desc.name]
            endpt.add_intrfc(is_)

    # switch interfaces
    for switch_desc in topo_cfg.switches:
        for intrfc in switch_desc.interfaces:
            # create a runtime representation from its desc representation
            is_ = net.create_intrfc_struct(intrfc)
            
            # save is for reference by id or name
            intrfc_by_id[is_.number] = is_
            intrfc_by_name[intrfc.name] = is_

            # store id -> name for trace
            tm.add_name(is_.number, intrfc.name, "interface")

            # for paramObj interface
            param_obj_by_id[is_.number] = is_
            param_obj_by_name[intrfc.name] = is_

            # look up switch, using switch name from desc representation
            swtch = switch_dev_by_name[switch_desc.name]
            swtch.add_intrfc(is_)

    # fetch the flow descriptions
    for flow_desc in topo_cfg.flows:
        # create a runtime representation from its desc representation
        bgf = create_bgf_struct(flow_desc)
        
        # nil returned if parameters don't support a flow (like zero rate)
        if bgf is None:
            continue
        
        # save pointer to net accessible by id or name
        flow_by_id[bgf.flow_id] = bgf
        flow_by_name[bgf.name] = bgf

        # for paramObj interface
        param_obj_by_id[bgf.number] = bgf
        param_obj_by_name[bgf.name] = bgf
        
        # store id -> name for trace
        tm.add_name(bgf.number, bgf.name, "flow")

    # link the connect fields, now that all interfaces are known
    # loop over routers
    for rtr_desc in topo_cfg.routers:
        # loop over interfaces the router endpoints
        for intrfc in rtr_desc.interfaces:
            # link the run-time representation of this interface to the
            # run-time representation of the interface it connects, if any
            # set the run-time pointer to the network faced by the interface
            net.link_intrfc_struct(intrfc)
    
    # loop over endpoints
    for endpt_desc in topo_cfg.endpts:
        # loop over interfaces the endpoint endpoints
        for intrfc in endpt_desc.interfaces:
            # link the run-time representation of this interface to the
            # run-time representation of the interface it connects, if any
            # set the run-time pointer to the network faced by the interface
            net.link_intrfc_struct(intrfc)

    # loop over switches
    for switch_desc in topo_cfg.switches:
        # loop over interfaces the switch endpoints
        for intrfc in switch_desc.interfaces:
            # link the run-time representation of this interface to the
            # run-time representation of the interface it connects, if any
            # set the run-time pointer to the network faced by the interface
            net.link_intrfc_struct(intrfc)

    # networks have slices with pointers with things that
    # we know now are initialized, so can finish the initialization
    
    # loop over networks
    for netd in topo_cfg.networks:
        # find the run-time representation of the network
        netobj = network_by_name[netd.name]

        # initialize it from the desc description of the network
        netobj.init_network_struct(netd)

    for dev in topo_dev_by_id.values():
        dev_id = dev.dev_id()
        
        # indices of directed_topo_graph are ids derived from position in 
        # directed_nodes slide index of the directed node is its identity.
        # The 'i' component of the intPair is its devID, j is 0 if the source,
        # 1 if dest index is devID, i component of intPair is directed id of
        # source, j is directed id of destination
        
        # create directed nodes
        src_node = net.intPair(i=dev_id, j=0)
        dst_node = net.intPair(i=dev_id, j=1)

        # obtain ids and remember them as mapped to by dev_id
        src_node_id = len(directed_nodes)
        directed_nodes.append(src_node)
        directed_topo_graph[src_node_id] = []
        
        dst_node_id = len(directed_nodes)
        directed_nodes.append(dst_node)
        directed_topo_graph[dst_node_id] = []
        dev_id_to_directed[dev_id] = net.intPair(i=src_node_id, j=dst_node_id)
        
        directed_id_to_dev[src_node_id] = dev_id
        directed_id_to_dev[dst_node_id] = dev_id
        
        # if the device is not an endpoint create an edge from 
        # destination node to source node
        if (dev.dev_type() is not net.DevCode.EndptCode):
            directed_topo_graph[dst_node_id].append(src_node_id)

    # put all the connections recorded in the Cabled and Wireless 
    # fields into the topo_graph
    for dev in topo_dev_by_id.values():
        dev_id = dev.dev_id()
        for intrfc in dev.dev_intrfcs():
            connected = False
            # cabled connection
            if (getattr(intrfc, 'cable', None) is not None \
                        and getattr(intrfc.cable, 'device', None) is not None \
                        and compatible_intrfcs(intrfc, intrfc.cable)
                ):

                peer_id = intrfc.cable.device.dev_id()
                connect_ids(topo_graph, dev_id, peer_id, 
                            intrfc.number, intrfc.cable.number)
                connect_directed_ids(directed_topo_graph, dev_id, peer_id)
                connected = True
            
            # carried connection
            if (not connected and hasattr(intrfc, 'carry') \
                            and len(intrfc.carry) > 0
            ):
                for cintrfc in intrfc.carry:
                    if compatible_intrfcs(intrfc, cintrfc):
                        peer_id = cintrfc.device.dev_id()
                        connect_ids(topo_graph, dev_id, peer_id, 
                                    intrfc.number, cintrfc.number)
                        connect_directed_ids(directed_topo_graph, dev_id, peer_id)
                        connected = True
            
            # wireless connection
            if (not connected and hasattr(intrfc, 'wireless') \
                and len(intrfc.wireless) > 0
            ):
                for conn in intrfc.wireless:
                    peer_id = conn.device.dev_id()
                    connect_ids(topo_graph, dev_id, peer_id, 
                                intrfc.number, conn.number)
                    connect_directed_ids(directed_topo_graph, dev_id, peer_id)


def create_bgf_struct(fd: desc_topo.FlowDesc) -> Optional[flow.Flow]:
    """
    create_bgf_struct creates a Flow object from a FlowDesc
    """
    return flow.create_flow(fd.name, fd.src_dev, fd.dst_dev, 
                           fd.req_rate, fd.frame_size, fd.mode, 
                           0, fd.groups)


# compatible_intrfcs checks whether the named pair of interfaces are compatible
# w.r.t. their state on cable, carry, and wireless
def compatible_intrfcs(intrfc1: net.intrfcStruct, intrfc2: net.intrfcStruct):
    if getattr(intrfc1, 'cable', None) is not None \
               and getattr(intrfc2, 'cable', None) is not None:
        return True
    return hasattr(intrfc1, 'carry') \
           and len(intrfc1.carry) > 0 \
           and hasattr(intrfc2, 'carry') \
           and len(intrfc2.carry) > 0

# add_topo_dev_lookup puts a new entry in the topo_dev_by_id and 
# topo_dev_by_name maps if that entry does not already exist
def add_topo_dev_lookup(td_id: int, td_name: str, td: net.TopoDev):
    global topo_dev_by_id, topo_dev_by_name
    if td_id in topo_dev_by_id:
        raise Exception(f"index {td_id} over-used in topo_dev_by_id\n")
    
    if td_name in topo_dev_by_name:
        raise Exception(f"name {td_name} over-used in topo_dev_by_name\n")
    
    topo_dev_by_id[td_id] = td
    topo_dev_by_name[td_name] = td

def initialize_bckgrnd_endpt(evt_mgr, endpt_dev):
    global u01_list, num_u01
    if not (endpt_dev.endpt_state.bckgrnd_rate > 0.0):
        return

    ts = endpt_dev.endpt_sched

    # only do this once
    if getattr(ts, 'bckgrnd_on', False):
        return

    rho = endpt_dev.endpt_state.bckgrnd_rate * endpt_dev.endpt_state.bckgrnd_srv

    # compute the initial number of busy cores
    busy = int(round(ts.cores * rho))

    # schedule the background task arrival process
    u01 = u01_list[endpt_dev.endpt_state.bckgrnd_idx]
    endpt_dev.endpt_state.bckgrnd_idx = (endpt_dev.endpt_state.bckgrnd_idx + 1) \
                                        % num_u01

    arrival = -math.log(1.0-u01) / endpt_dev.endpt_state.bckgrnd_rate
    evt_mgr.schedule(endpt_dev, None, scheduler.add_bckgrnd,
                     vrtime.seconds_to_time(arrival))
    
    # set some cores busy
    for idx in range(busy):
        ts.in_bckgrnd += 1
        u01 = u01_list[endpt_dev.endpt_state.bckgrnd_idx]
        endpt_dev.endpt_state.bckgrnd_idx = (endpt_dev.endpt_state.bckgrnd_idx + 1) \
                                            % num_u01
        service = -endpt_dev.endpt_state.bckgrnd_srv * math.log(1.0-u01)
        evt_mgr.schedule(endpt_dev, None, scheduler.rm_bckgrnd, 
                         vrtime.seconds_to_time(service))

    ts.bckgrnd_on = True


def initialize_bckgrnd(evt_mgr: evtm.EventManager):
    global endpt_dev_by_name
    for endpt_dev in endpt_dev_by_name.values():
        initialize_bckgrnd_endpt(evt_mgr, endpt_dev)

