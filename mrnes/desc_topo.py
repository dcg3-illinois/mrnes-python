import json
import yaml
import os
import copy
from typing import List, Dict, Optional, Any
from abc import ABC, abstractmethod

# file desc-topo.py holds structs, methods, and data structures supporting
# the construction of and access to models of computers and networks using
# the mrnes API


# A DevExecDesc struct holds a description of a device operation timing.
# ExecTime is the time (in seconds), it depends on attribute Model
class DevExecDesc:
    """
    Holds a description of a device operation timing.
    ExecTime is the time (in seconds), it depends on attribute Model.
    """
    DevOp: str
    Model: str
    PcktLen: int
    ExecTime: float
    Bndwdth: float
    def __init__(self, DevOp: str, Model: str, PcktLen: int, ExecTime: float, Bndwdth: float):
        self.DevOp: str = DevOp
        self.Model: str = Model
        self.PcktLen: int = PcktLen
        self.ExecTime: float = ExecTime
        self.Bndwdth: float = Bndwdth

# A DevExecList holds a map (Times) whose key is the operation
# of a device, and whose value is a list of DevExecDescs
# associated with that operation.
class DevExecList:
    """
    Holds a map (Times) whose key is the operation of a device, and whose value is a list of DevExecDescs associated with that operation.
    """
    ListName: str
    Times: Dict[str, List[DevExecDesc]]
    # ListName is an identifier for this collection of timings
    def __init__(self, ListName: str):
        self.ListName: str = ListName
        # key is the device operation.  Each has a list
        # of descriptions of the timing of that operation, as a function of device model
        self.Times: Dict[str, List[DevExecDesc]] = {}

    # WriteToFile stores the DevExecList struct to the file whose name is given.
    # Serialization to json or to yaml is selected based on the extension of this name.
    def WriteToFile(self, filename: str) -> Optional[Exception]:
        pathExt = os.path.splitext(filename)[1]
        bytestr = None
        if pathExt in [".yaml", ".YAML", ".yml"]:
            try:
                # Use yaml.dump with default_flow_style=False for readability
                bytestr = yaml.dump(self, default_flow_style=False)
            except Exception as e:
                raise e
        elif pathExt in [".json", ".JSON"]:
            try:
                bytestr = json.dumps(self, default=lambda o: o.__dict__, indent=4)
            except Exception as e:
                raise e
        else:
            raise Exception("Unsupported file extension")
        try:
            with open(filename, "w") as f:
                f.write(bytestr)
        except Exception as e:
            raise e
        return None

    # AddTiming takes the parameters of a DevExecDesc, creates one, and adds it to the FuncExecList
    def AddTiming(self, devOp: str, model: str, execTime: float, pcktlen: int, bndwdth: float):
        if devOp not in self.Times:
            self.Times[devOp] = []
        self.Times[devOp].append(DevExecDesc(devOp, model, pcktlen, execTime, bndwdth))

# CreateDevExecList is an initialization constructor.
# Its output struct has methods for integrating data.
def CreateDevExecList(listname: str) -> 'DevExecList':
    delist = DevExecList(listname)
    return delist


# ReadDevExecList deserializes a byte slice holding a representation of an DevExecList struct.
# If the input argument of dict (those bytes) is empty, the file whose name is given is read
# to acquire them.  A deserialized representation is returned, or an error if one is generated
# from a file read or the deserialization.
def ReadDevExecList(filename: str, useYAML: bool, dict_bytes: bytes) -> Optional['DevExecList']:
    if not dict_bytes:
        with open(filename, "rb") as f:
            dict_bytes = f.read()
    
    if useYAML:
        data = yaml.safe_load(dict_bytes)
    else:
        data = json.loads(dict_bytes)

    delist = DevExecList(data.get('ListName', ''))
    times = data.get('Times', {})
    for op, descs in times.items():
        delist.Times[op] = [DevExecDesc(**desc) for desc in descs]
    return delist

# numberOfIntrfcs (and more generally, numberOf{Objects}
# are counters of the number of default instances of each
# object type have been created, and so can be used
# to help create unique default names for these objects
#
#   Not currently used at initialization, see if useful for the simulation
numberOfIntrfcs: int = 0
numberOfRouters: int = 0
numberOfSwitches: int = 0
numberOfEndpts: int = 0
numberOfFlows: int = 0

# maps that let you use a name to look up an object
objTypeByName: Dict[str, str] = {}
devByName: Dict[str, NetDevice] = {}
netByName: Dict[str, NetworkFrame] = {}
rtrByName: Dict[str, RouterFrame] = {}
flowByName: Dict[str, FlowFrame] = {}

# devConnected gives for each NetDev device a list of the other NetDev devices
# it connects to through wired interfaces
devConnected: Dict[str, List[str]] = {}

# not really useful for this implementation, but keeping it to maintain consistency with go
def InitTopoDesc() -> None:
    global numberOfIntrfcs, numberOfRouters, numberOfSwitches, numberOfEndpts, numberOfFlows
    numberOfIntrfcs: int = 0
    numberOfRouters: int = 0
    numberOfSwitches: int = 0
    numberOfEndpts: int = 0
    numberOfFlows: int = 0
    # maps that let you use a name to look up an object
    global objTypeByName, devByName, netByName, rtrByName, flowByName, devConnected
    objTypeByName = {}
    devByName = {}
    netByName = {}
    rtrByName = {}
    flowByName = {}
    # devConnected gives for each NetDev device a list of the other NetDev devices
    # it connects to through wired interfaces
    devConnected = {}

# To most easily serialize and deserialize the various structs involved in creating
# and communicating a simulation model, we ensure that they are all completely
# described without pointers, every structure is fully instantiated in the description.
# On the other hand it is easily to manage the construction of complicated structures
# under the rules Golang uses for memory management if we allow pointers.
# Our approach then is to define two respresentations for each kind of structure.  One
# has the final appellation of 'Frame', and holds pointers.  The pointer free version
# has the final  appellation of 'Desc'.   After completely building the structures using
# Frames we transform each into a Desc version for serialization.

# The NetDevice interface lets us use common code when network objects
# (endpt, switch, router, network, flow) are involved in model construction.
#
# In Python, this is represented as an abstract base class.
class NetDevice(ABC):
    @abstractmethod
    def DevName(self) -> str:
        """returns the .Name field of the struct"""
        pass

    @abstractmethod
    def DevID(self) -> str:
        """returns a unique (string) identifier for the struct"""
        pass

    @abstractmethod
    def DevType(self) -> str:
        """returns the type ("Switch","Router","Endpt","Network")"""
        pass

    @abstractmethod
    def DevInterfaces(self) -> list[IntrfcFrame]:
        """list of interfaces attached to the NetDevice, if any"""
        pass

    @abstractmethod
    def DevAddIntrfc(self, intrfc: IntrfcFrame) -> None:
        """function to add another interface to the netDevice"""
        pass



# IntrfcDesc defines a serializable description of a network interface
class IntrfcDesc:
    def __init__(self, Name: str = "", Groups: List[str] = None, DevType: str = "", MediaType: str = "", Device: str = "", Cable: str = "", Carry: List[str] = None, Wireless: List[str] = None, Faces: str = ""):
        # name for interface, unique among interfaces on endpting device.
        self.Name: str = Name
        
        self.Groups: List[str] = Groups if Groups is not None else []
        
        # type of device that is home to this interface, i.e., "Endpt", "Switch", "Router"
        self.DevType: str = DevType
        
        # whether media used by interface is 'wired' or 'wireless' .... could put other kinds here, e.g., short-wave, satellite
        self.MediaType: str = MediaType
        
        # name of endpt, switch, or router on which this interface is resident
        self.Device: str = Device
        
        # name of interface (on a different device) to which this interface is directly (and singularly) connected
        self.Cable: str = Cable
        
        # name of interface (on a different device) to which this interface is directly (and singularly) carried if wired and not on Cable
        self.Carry: List[str] = Carry if Carry is not None else []
        
        # list of names of interface (on a different device) to which this interface is connected through wireless
        self.Wireless: List[str] = Wireless if Wireless is not None else []
        
        # name of the network the interface connects to. There is a tacit assumption then that interface reaches routers on the network
        self.Faces: str = Faces

# IntrfcFrame gives a pre-serializable description of an interface, used in model construction.
# 'Almost' the same as IntrfcDesc, with the exception of one pointer
class IntrfcFrame:
    def __init__(self, Name: str = "", Groups: List[str] = None, DevType: str = "", MediaType: str = "", Device: str = "", Cable: Optional['IntrfcFrame'] = None, Carry: List['IntrfcFrame'] = None, Wireless: List['IntrfcFrame'] = None, Faces: str = ""):
        # name for interface, unique among interfaces on endpting device.
        self.Name: str = Name
        
        self.Groups: List[str] = Groups if Groups is not None else []
        
        # type of device that is home to this interface, i.e., "Endpt", "Switch", "Router"
        self.DevType: str = DevType
        
        # whether media used by interface is 'wired' or 'wireless' .... could put other kinds here, e.g., short-wave, satellite
        self.MediaType: str = MediaType
        
        # name of endpt, switch, or router on which this interface is resident
        self.Device: str = Device
        
        # pointer to interface (on a different device) to which this interface is directly (and singularly) connected.
        # this interface and the one pointed to need to have media type "wired"
        self.Cable: Optional['IntrfcFrame'] = Cable
        
        # pointer to interface (on a different device) to which this interface is directly if wired and not Cable.
        # this interface and the one pointed to need to have media type "wired", and have "Cable" be empty
        self.Carry: List['IntrfcFrame'] = Carry if Carry is not None else []
        
        # A wireless interface may connect to may devices, this slice points to those that can be reached
        self.Wireless: List['IntrfcFrame'] = Wireless if Wireless is not None else []
        
        # name of the network the interface connects to. We do not require that the media type of the interface be the same
        # as the media type of the network.
        self.Faces: str = Faces

    # AddGroup adds a group name to the interface's group list if not already present
    def AddGroup(self, groupName: str) -> None:
        if groupName not in self.Groups:
            self.Groups.append(groupName)

    # Transform converts this IntrfcFrame and returns an IntrfcDesc, for serialization.
    def Transform(self) -> 'IntrfcDesc':
        # most attributes are straight copies
        intrfcDesc = IntrfcDesc()
        intrfcDesc.Device = self.Device
        intrfcDesc.Name = self.Name
        intrfcDesc.DevType = self.DevType
        intrfcDesc.MediaType = self.MediaType
        intrfcDesc.Faces = self.Faces
        intrfcDesc.Groups = self.Groups
        intrfcDesc.Carry = []
        intrfcDesc.Wireless = []
        # a IntrfcDesc defines its Cable field to be a string, which we set 
        # here to be the name of the interface the IntrfcFrame version points to
        if self.Cable is not None:
            intrfcDesc.Cable = self.Cable.Name
        # a IntrfcDesc defines its Carry field to be a string, which we set
        # here to be the name of the interface the IntrfcFrame version points to
        for carry in self.Carry:
            intrfcDesc.Carry.append(carry.Name)
        
        for connection in self.Wireless:
            intrfcDesc.Wireless.append(connection.Name)

        return intrfcDesc

# DefaultIntrfcName generates a unique string to use as a name for an interface.
# That name includes the name of the device endpting the interface and a counter
def DefaultIntrfcName(device: str) -> str:
    global numberOfIntrfcs
    return f"intrfc@{device}[.{numberOfIntrfcs}]"

# CableIntrfcFrames links two interfaces through their 'Cable' attributes
def CableIntrfcFrames(intrfc1: IntrfcFrame, intrfc2: IntrfcFrame):
    intrfc1.Cable = intrfc2
    intrfc2.Cable = intrfc1

# CarryIntrfcFrames links two interfaces through their 'Cable' attributes
def CarryIntrfcFrames(intrfc1: IntrfcFrame, intrfc2: IntrfcFrame):
    found = any(carry.Name == intrfc2.Name for carry in intrfc1.Carry)
    if not found:
        intrfc1.Carry.append(intrfc2)
    found = any(carry.Name == intrfc1.Name for carry in intrfc2.Carry)
    if not found:
        intrfc2.Carry.append(intrfc1)


def CreateIntrfc(device: str, name: str, devType: str, mediaType: str, faces: str) -> 'IntrfcFrame':
    """
    Constructor for IntrfcFrame that fills in most of the attributes except Cable.
    Arguments name the device holding the interface and its type, the type of communication fabric the interface uses, and the
    name of the network the interface connects to
    """
    intrfc = IntrfcFrame()
    
    global numberOfIntrfcs
    # counter used in the generation of default names
    numberOfIntrfcs += 1
    
    # an empty string given as name flags that we should create a default one.
    if len(name) == 0:
        intrfc.Name = DefaultIntrfcName(device)
    else:
        intrfc.Name = name
    
    # fill in structure attributes included in function call
    intrfc.Device = device
    intrfc.DevType = devType
    intrfc.MediaType = mediaType
    intrfc.Faces = faces
    intrfc.Wireless = []
    intrfc.Groups = []
    
    # if the device in which this interface is embedded is not a router we are done
    if devType != "Router":
        return intrfc
    
    # embedded in a router. Get its frame and that of the network which is faced
    rtr = devByName[device]  # type: ignore
    net = netByName[faces]   # type: ignore
    
    # before adding the router to the network's list of routers, check for duplication (based on the router's name)
    duplicated = False
    for stored in net.Routers:
        if rtr.Name == stored.Name:
            duplicated = True
            
            break
    
    # OK to save
    if not duplicated:
        net.Routers.append(rtr)
    
    return intrfc

# isConnected is part of a set of functions and data structures useful in managing
# construction of a communication network. It indicates whether two devices whose
# identities are given are already connected through their interfaces, by Cable, Carry, or Wireless
def isConnected(id1: str, id2: str) -> bool:
    if id1 not in devConnected:
        return False
    
    for peerID in devConnected[id1]:
        if peerID == id2:
            return True
    
    return False

# markConnected modifies the devConnected data structure to reflect that
# the devices whose identities are the arguments have been connected.
def markConnected(id1: str, id2: str) -> None:
    # if already connected there is nothing to do here
    if isConnected(id1, id2):
        return
    
    # for both devices, add their names to the 'connected to' list of the other
    if id1 not in devConnected:
        devConnected[id1] = []
    devConnected[id1].append(id2)
    if id2 not in devConnected:
        devConnected[id2] = []
    devConnected[id2].append(id1)

# determine whether intrfc1 is in the Carry slice of intrfc2
def carryContained(intrfc1: 'IntrfcFrame', intrfc2: 'IntrfcFrame') -> bool:
    for intrfc in intrfc2.Carry:
        if intrfc == intrfc1 or intrfc.Name == intrfc1.Name:
            return True
    return False

# ConnectDevs establishes a 'cabled' or 'carry' connection (creating interfaces if needed) between
# devices dev1 and dev2 (recall that NetDevice is an interface satisified by Endpt, Router, Switch)
def ConnectDevs(dev1: NetDevice, dev2: NetDevice, cable: bool, faces: str) -> None:
    # if already connected we don't do anything
    if isConnected(dev1.DevID(), dev2.DevID()):
        return
    
    # this call will record the connection
    markConnected(dev1.DevID(), dev2.DevID())

    # ensure that both devices are known to the network
    net = netByName[faces]
    err = net.IncludeDev(dev1, "wired", True)
    if err is not None:
        raise Exception(err)
    err = net.IncludeDev(dev2, "wired", True)
    if err is not None:
        raise Exception(err)
    
    # for each device collect all the interfaces that face the named 
    # network and are not wireless
    intrfcs1 = []
    intrfcs2 = []
    for intrfc in dev2.DevInterfaces():
        if intrfc.Faces == faces and intrfc.MediaType != "wireless":
            intrfcs2.append(intrfc)
    for intrfc in dev1.DevInterfaces():
        if intrfc.Faces == faces and intrfc.MediaType != "wireless":
            intrfcs1.append(intrfc)
    
    # check whether the connection requested exists already or we 
    # can complete it without creating new interfaces
    if cable:
        for intrfc1 in intrfcs1:
            for intrfc2 in intrfcs2:
                # keep looping if we're looking for cable and they don't match
                if cable and intrfc1.Cable is not None and intrfc1.Cable != intrfc2:
                    continue
                
                # either intrfc1.cable is None or intrfc is connected already to intrfc2.
                # so then if intrfc2.cable is None or is connected to intrfc1 we can complete the connection and leave
                if cable and (intrfc2.Cable == intrfc1 or intrfc2.Cable is None):
                    intrfc1.Cable = intrfc2
                    intrfc2.Cable = intrfc1
                    return
    else:
        # see whether we can establish the connection without new interfaces
        for intrfc1 in intrfcs1:
            for intrfc2 in intrfcs2:
                if carryContained(intrfc1, intrfc2) and not carryContained(intrfc2, intrfc1):
                    # put intrfc2 in intrfc1's Carry slice
                    intrfc2.Carry.append(intrfc1)
                    return
                if carryContained(intrfc2, intrfc1) and not carryContained(intrfc1, intrfc2):
                    # put intrfc1 in intrfc2's Carry slice
                    intrfc1.Carry.append(intrfc2)
                    return
                
    # no prior reason to complete connection between dev1 and dev2
    # see whether each has a 'free' interface, meaning it points to the right network but is not yet cabled or carried
    free1 = None
    free2 = None
    
    # check dev1's interfaces
    for intrfc1 in intrfcs1:
        # we're looking to cable, the interface is facing the right network, and it's cabling is empty
        if intrfc1.Faces == faces and intrfc1.Cable is None:
            free1 = intrfc1
            break
    
    # if dev1 does not have a free interface, create one
    if free1 is None:
        intrfcName = DefaultIntrfcName(dev1.DevName())
        free1 = CreateIntrfc(dev1.DevName(), intrfcName, dev1.DevType(), "wired", faces)
        err = dev1.DevAddIntrfc(free1)
        if err is not None:
            raise Exception(err)
    
    # check dev2's interfaces
    for intrfc2 in intrfcs2:
        if intrfc2.Faces == faces and intrfc2.Cable is None:
            free2 = intrfc2
            break

    # if dev2 does not have a free interface, create one
    if free2 is None:
        intrfcName = DefaultIntrfcName(dev2.DevName())
        free2 = CreateIntrfc(dev2.DevName(), intrfcName, dev2.DevType(), "wired", faces)
        err = dev2.DevAddIntrfc(free2)
        if err is not None:
            raise Exception(err)
    
    # found the interfaces, make the connection, using cable or carry as directed by the input argument
    if cable:
        free1.Cable = free2
        free2.Cable = free1
    else:
        free1.Carry.append(free2)
        free2.Carry.append(free1)

# DevNetworks returns a comma-separated string of
# the names of networks the argument NetDevice interfaces face
def DevNetworks(dev: NetDevice) -> str:
    nets = []
    for intrfc in dev.DevInterfaces():
        nets.append(intrfc.Faces)
    return ",".join(nets)

# CreateNetwork is a constructor, with all the inherent attributes specified
def CreateNetwork(name: str, NetScale: str, MediaType: str) -> 'NetworkFrame':
    nf = NetworkFrame(name, NetScale, MediaType)
    # initialize slices
    nf.Routers = []
    nf.Switches = []
    nf.Endpts = []
    nf.Groups = []
    objTypeByName[name] = "Network"  # object name gets you object type
    netByName[name] = nf              # network name gets you network frame
    return nf


# A NetworkFrame holds the attributes of a network during the model construction phase
class NetworkFrame(NetDevice):
    def __init__(self, Name: str = "", NetScale: str = "", MediaType: str = ""):
        # Name is a unique name across all objects in the simulation. It is used universally to reference this network
        self.Name: str = Name
        
        self.Groups: list[str] = []
        
        # NetScale describes role of network, e.g., LAN, WAN, T3, T2, T1.  Used as an attribute when doing experimental configuration
        self.NetScale: str = NetScale
        
        # for now the network is either "wired" or "wireless"
        self.MediaType: str = MediaType
        
        # any router with an interface that faces this network is in this list
        self.Routers: list['RouterFrame'] = []
        
        # any endpt with an interface that faces this network is in this list
        self.Endpts: list['EndptFrame'] = []
        
        # any switch with an interface that faces this network is in this list
        self.Switches: list['SwitchFrame'] = []

    # FacedBy determines whether the device offered as an input argument
    # has an interface whose 'Faces' component references this network
    def FacedBy(self, dev: NetDevice) -> bool:
        intrfcs = dev.DevInterfaces()
        netName = self.Name
        for intrfc in intrfcs:
            if intrfc.Faces == netName:
                return True
        return False

    # AddGroup appends a group name to the network frame list of groups,
    # checking first whether it is already present in the list
    def AddGroup(self, groupName: str) -> None:
        if groupName not in self.Groups:
            self.Groups.append(groupName)

    # IncludeDev makes sure that the network device being offered
    #   a) has an interface facing the network
    #   b) is included in the network's list of those kind of devices
    def IncludeDev(self, dev: NetDevice, mediaType: str, chkIntrfc: bool) -> Optional[Exception]:
        devName = dev.DevName()
        devType = dev.DevType()

        intrfc = None

        # check consistency of network media type and mediaType argument.
        # If mediaType is wireless then network must be wireless.   It is
        # permitted though to have the network be wireless and the mediaType be wired.
        if mediaType == "wireless" and self.MediaType != "wireless":
            return Exception("including a wireless device in a wired network")
        
        # if the device does not have an interface pointed at the network, make one
        if chkIntrfc and not self.FacedBy(dev):
            # create the interface
            intrfcName = DefaultIntrfcName(dev.DevName())
            intrfc = CreateIntrfc(devName, intrfcName, devType, mediaType, self.Name)
       
        # add it to the right list in the network
        if devType == "Endpt":
            endpt = dev  # type: ignore
            if intrfc is not None:
                endpt.Interfaces.append(intrfc)

            if not endptPresent(self.Endpts, endpt):
                self.Endpts.append(endpt)

        elif devType == "Router":
            rtr = dev  # type: ignore
            if intrfc is not None:
                rtr.Interfaces.append(intrfc)

            if not routerPresent(self.Routers, rtr):
                self.Routers.append(rtr)
        
        elif devType == "Switch":
            swtch = dev  # type: ignore
            if intrfc is not None:
                swtch.Interfaces.append(intrfc)

            if not switchPresent(self.Switches, swtch):
                self.Switches.append(swtch)

        return None

    # AddRouter includes the argument router into the network
    def AddRouter(self, rtrf: 'RouterFrame') -> Optional[Exception]:
        # check whether a router with this same name already exists here
        for rtr in self.Routers:
            if rtr.Name == rtrf.Name:
                return None
        if not self.FacedBy(rtrf):
            return Exception(f"attempting to add router {rtrf.Name} to network {self.Name} without prior association of an interface")
        self.Routers.append(rtrf)
        return None

    # AddSwitch includes the argument switch into the network
    def AddSwitch(self, swtch: 'SwitchFrame') -> Optional[Exception]:
        for nfswtch in self.Switches:
            if nfswtch.Name == swtch.Name:
                return None
        
        if not self.FacedBy(swtch):
            return Exception(f"attempting to add switch {swtch.Name} to network {self.Name} without prior association of an interface")
        
        self.Switches.append(swtch)
        return None

    # Transform converts a network frame into a network description.
    # It copies string attributes, and converts pointers to routers, and switches
    # to strings with the names of those entities
    def Transform(self) -> 'NetworkDesc':
        nd = NetworkDesc()
        nd.Name = self.Name
        nd.NetScale = self.NetScale
        nd.MediaType = self.MediaType
        nd.Groups = self.Groups
        nd.Routers = [rtr.Name for rtr in self.Routers]
        nd.Endpts = [endpt.Name for endpt in self.Endpts]
        nd.Switches = [swtch.Name for swtch in self.Switches]
        return nd


# RouterDesc describes parameters of a Router in the topology.
class RouterDesc:
    def __init__(self):
        # Name is unique string identifier used to reference the router
        self.Name: str = ""
        
        self.Groups: list[str] = []
        
        # Model is an attribute like "Cisco 6400". Used primarily in run-time configuration
        self.Model: str = ""
        
        self.OpDict: dict[str, str] = {}
        
        # list of names interfaces that describe the ports of the router
        self.Interfaces: list[IntrfcDesc] = []

# DefaultRouterName returns a unique name for a router
def DefaultRouterName() -> str:
    global numberOfRouters
    return f"rtr.[{numberOfRouters}]"

# CreateRouter is a constructor, stores (possibly creates default) name, initializes slice of interface frames
def CreateRouter(name: str, model: str) -> 'RouterFrame':
    global numberOfRouters
    rtr = RouterFrame()
    numberOfRouters += 1
    
    rtr.Model = model
    
    if len(name) == 0:
        rtr.Name = DefaultRouterName()
    else:
        rtr.Name = name
    
    objTypeByName[rtr.Name] = "Router"
    devByName[rtr.Name] = rtr
    rtrByName[rtr.Name] = rtr
    rtr.Interfaces = []
    rtr.Groups = []
    return rtr

# routerPresent returns a boolean flag indicating whether
# a router provided as input exists already in a list of routers
# also provided as input
def routerPresent(rtrList: list['RouterFrame'], rtr: 'RouterFrame') -> bool:
    for rtrInList in rtrList:
        if rtrInList.Name == rtr.Name:
            return True
    return False

# RouterFrame describes parameters of a Router in the topology in pre-serialized form
class RouterFrame(NetDevice):
    def __init__(self, Name: str = "", Model: str = ""):
        self.Name: str = Name
        self.Groups: list[str] = []
        self.Model: str = Model
        self.OpDict: dict[str, str] = {}
        self.Interfaces: list[IntrfcFrame] = []

    # NetDevice interface methods
    def DevName(self) -> str:
        return self.Name
    
    def DevType(self) -> str:
        return "Router"
    
    def DevID(self) -> str:
        return self.Name
    
    def DevModel(self) -> str:
        return self.Model
    
    def DevInterfaces(self) -> list[IntrfcFrame]:
        return self.Interfaces
    
    def AddIntrfc(self, intrfc: IntrfcFrame) -> Optional[Exception]:
        for ih in self.Interfaces:
            if ih == intrfc or ih.Name == intrfc.Name:
                return Exception(f"attempt to re-add interface {intrfc.Name} to switch {self.Name}")
        
        # ensure that the interface has stored the home device type and name
        intrfc.Device = self.Name
        intrfc.DevType = "Router"
        self.Interfaces.append(intrfc)
        
        return None
    
    def DevAddIntrfc(self, iff: IntrfcFrame) -> Optional[Exception]:
        return self.AddIntrfc(iff)
    
    def AddGroup(self, groupName: str) -> None:
        if groupName not in self.Groups:
            self.Groups.append(groupName)
    
    def Transform(self) -> RouterDesc:
        rd = RouterDesc()
        rd.Name = self.Name
        rd.Model = self.Model
        rd.Groups = self.Groups
        rd.OpDict = self.OpDict

        # create serializable representation of the interfaces by calling the Transform method on their Frame representation
        rd.Interfaces = [iface.Transform() for iface in self.Interfaces]
        return rd

    # WirelessConnectTo establishes a wireless connection (creating interfaces if needed)
    # between this router (hub) and a device
    def WirelessConnectTo(self, dev: NetDevice, faces: str) -> Optional[Exception]:
        # ensure that both devices are known to the network
        net = netByName[faces]
        err = net.IncludeDev(self, "wireless", True)
        if err is not None:
            return err
        
        err = net.IncludeDev(dev, "wireless", True)
        if err is not None:
            return err
        
        # ensure that hub has wireless interface facing the named network
        hubIntrfc = None
        for intrfc in self.Interfaces:
            if intrfc.MediaType == "wireless" and intrfc.Faces == faces:
                hubIntrfc = intrfc
                break

        # create an interface if necessary
        if hubIntrfc is None:
            intrfcName = DefaultIntrfcName(self.DevName())
            hubIntrfc = CreateIntrfc(self.DevName(), intrfcName, self.DevType(), "wireless", faces)
            err = self.DevAddIntrfc(hubIntrfc)
            if err is not None:
                raise Exception(err)
        
        # ensure that device has wireless interface facing the named network
        devIntrfc = None
        for intrfc in dev.DevInterfaces():
            if intrfc.MediaType == "wireless" and intrfc.Faces == faces:
                devIntrfc = intrfc
                break
        
        # create an interface if necessary
        if devIntrfc is None:
            intrfcName = DefaultIntrfcName(dev.DevName())
            devIntrfc = CreateIntrfc(dev.DevName(), intrfcName, dev.DevType(), "wireless", faces)
            err = dev.DevAddIntrfc(devIntrfc)
            if err is not None:
                raise Exception(err)
        
        hubIntrfcName = hubIntrfc.Name
        devIntrfcName = devIntrfc.Name

        # check whether dev is already known to hub
        devKnown = any(hconn.Name == devIntrfcName for hconn in hubIntrfc.Wireless)

        # check whether hub is already known to dev
        hubKnown = any(dconn.Name == hubIntrfcName for dconn in devIntrfc.Wireless)
        
        # add as needed
        if not devKnown:
            hubIntrfc.Wireless.append(devIntrfc)
        if not hubKnown:
            devIntrfc.Wireless.append(hubIntrfc)
        return None

# DefaultFlowName returns a unique name for a flow
def DefaultFlowName() -> str:
    global numberOfFlows
    return f"flow.[{numberOfFlows}]"

# CreateFlowFrame is a constructor, stores (possibly creates default) name
def CreateFlowFrame(name: str, srcDev: str, dstDev: str, mode: str, reqRate: float, frameSize: int) -> 'FlowFrame':
    global numberOfFlows
    ff = FlowFrame()
    numberOfFlows += 1

    if len(name) == 0:
        ff.Name = FlowFrame.DefaultFlowName()
    else:
        ff.Name = name

    ff.SrcDev = srcDev
    ff.DstDev = dstDev
    ff.Mode = mode
    ff.ReqRate = reqRate
    ff.FrameSize = frameSize
    
    objTypeByName[ff.Name] = "Flow"
    devByName[ff.Name] = ff
    flowByName[ff.Name] = ff
    ff.Groups = []
    return ff

# FlowFrame describes parameters of a Flow in the topology in pre-serialized form
class FlowFrame(NetDevice):
    def __init__(self, Name: str = "", SrcDev: str = "", DstDev: str = "", Mode: str = "", ReqRate: float = 0.0, FrameSize: int = 0):
        self.Name: str = Name
        self.SrcDev: str = SrcDev
        self.DstDev: str = DstDev
        self.Mode: str = Mode
        self.ReqRate: float = ReqRate
        self.FrameSize: int = FrameSize
        self.Groups: list[str] = []
    
    # NetDevice interface methods
    def DevName(self) -> str:
        return self.Name
    
    def DevType(self) -> str:
        return "Flow"
    
    def DevID(self) -> str:
        return self.Name
    
    def DevModel(self) -> str:
        return ""
    
    def DevInterfaces(self) -> list[IntrfcFrame]:
        return []
    
    def DevAddIntrfc(self, iff: IntrfcFrame) -> Optional[Exception]:
        return None
    
    def AddGroup(self, groupName: str) -> None:
        if groupName not in self.Groups:
            self.Groups.append(groupName)
    
    def Transform(self) -> 'FlowDesc':
        fd = FlowDesc()
        fd.Name = self.Name
        fd.SrcDev = self.SrcDev
        fd.DstDev = self.DstDev
        fd.Mode = self.Mode
        fd.ReqRate = self.ReqRate
        fd.FrameSize = self.FrameSize
        fd.Groups = self.Groups
        return fd


# FlowDesc describes parameters of a Flow in the topology.
class FlowDesc:
    def __init__(self):
        # Name is unique string identifier used to reference the flow
        self.Name: str = ""
        self.SrcDev: str = ""
        self.DstDev: str = ""
        self.Mode: str = ""
        self.ReqRate: float = 0.0
        self.FrameSize: int = 0
        self.Groups: List[str] = []

# SwitchDesc holds a serializable representation of a switch.
class SwitchDesc:
    def __init__(self):
        self.Name: str = ""
        self.Groups: List[str] = []
        self.Model: str = ""
        self.OpDict: Dict[str, str] = {}
        self.Interfaces: List['IntrfcDesc'] = []


def DefaultSwitchName(name: str) -> str:
    global numberOfSwitches
    return f"switch({name}).{numberOfSwitches}"


def CreateSwitch(name: str, model: str) -> 'SwitchFrame':
    global numberOfSwitches, objTypeByName, devByName
    sf = SwitchFrame()
    numberOfSwitches += 1
    
    if len(name) == 0:
        sf.Name = DefaultSwitchName("switch")
    else:
        sf.Name = name
    
    objTypeByName[sf.Name] = "Switch"
    devByName[sf.Name] = sf

    sf.Model = model
    sf.Interfaces = []
    sf.Groups = []
    
    return sf

# CreateHub constructs a switch frame tagged as being a hub
def CreateHub(name: str, model: str) -> 'SwitchFrame':
    hub = CreateSwitch(name, model)
    hub.AddGroup("Hub")
    return hub

# CreateBridge constructs a switch frame tagged as being a hub
def CreateBridge(name: str, model: str) -> 'SwitchFrame':
    bridge = CreateSwitch(name, model)
    bridge.AddGroup("Bridge")
    return bridge

# CreateRepeater constructs a switch frame tagged as being a repeater
def CreateRepeater(name: str, model: str) -> 'SwitchFrame':
    rptr = CreateSwitch(name, model)
    rptr.AddGroup("Repeater")
    return rptr

# switchPresent returns a boolean flag indicating whether
# a switch provided as input exists already in a list of switches
# also provided as input
def switchPresent(swtchList: List['SwitchFrame'], swtch: 'SwitchFrame') -> bool:
    for swtchInList in swtchList:
        if swtchInList.Name == swtch.Name:
            return True
    return False

# SwitchFrame holds a pre-serialization representation of a Switch
class SwitchFrame(NetDevice):
    def __init__(self, Name: str = "", Model: str = ""):
        self.Name: str = Name
        self.Groups: List[str] = []
        self.Model: str = Model
        self.OpDict: Dict[str, str] = {}
        self.Interfaces: List['IntrfcFrame'] = []

    # AddIntrfc includes a new interface frame for the switch.  Error is returned
    # if the interface (or one with the same name) is already attached to the SwitchFrame
    def AddIntrfc(self, iff: 'IntrfcFrame') -> Optional[Exception]:
        # check whether interface exists here already
        for ih in self.Interfaces:
            if ih == iff or ih.Name == iff.Name:
                return Exception(f"attempt to re-add interface {iff.Name} to switch {self.Name}")
        
        # ensure that the interface has stored the home device type and name
        iff.Device = self.Name
        iff.DevType = "Switch"
        self.Interfaces.append(iff)

        return None
 
    # AddGroup adds a group to a switch's list of groups, if not already present in that list
    def AddGroup(self, groupName: str) -> None:
        if groupName not in self.Groups:
            self.Groups.append(groupName)

    # NetDevice interface methods
    def DevName(self) -> str:
        return self.Name
    
    def DevType(self) -> str:
        return "Switch"
    
    def DevID(self) -> str:
        return self.Name
    
    def DevInterfaces(self) -> List['IntrfcFrame']:
        return self.Interfaces
    
    def DevAddIntrfc(self, iff: 'IntrfcFrame') -> Optional[Exception]:
        return self.AddIntrfc(iff)
    
    # Transform returns a serializable SwitchDesc, transformed from a SwitchFrame.
    def Transform(self) -> 'SwitchDesc':
        sd = SwitchDesc()
        sd.Name = self.Name
        sd.Model = self.Model
        sd.Groups = copy.deepcopy(self.Groups)
        sd.OpDict = self.OpDict

        sd.Interfaces = [iface.Transform() for iface in self.Interfaces]
        
        return sd

# EndptDesc defines serializable representation of an endpoint.
class EndptDesc:
    def __init__(self):
        self.Name: str = ""
        self.Groups: List[str] = []
        self.Model: str = ""
        self.Cores: int = 0
        self.Accel: Dict[str, str] = {}
        self.Interfaces: List['IntrfcDesc'] = []

# EndptFrame defines pre-serialization representation of an Endpt
class EndptFrame(NetDevice):
    def __init__(self, Name: str = "", Model: str = "", Cores: int = 0):
        self.Name: str = Name
        self.Groups: List[str] = []
        self.Model: str = Model
        self.Cores: int = Cores
        self.Accel: Dict[str, str] = {}
        self.Interfaces: List['IntrfcFrame'] = []

    def AddGroup(self, groupName: str) -> None:
        if groupName not in self.Groups:
            self.Groups.append(groupName)

    # NetDevice interface methods
    def DevName(self) -> str:
        return self.Name
    
    def DevType(self) -> str:
        return "Endpt"
    
    def DevID(self) -> str:
        return self.Name
    
    def DevInterfaces(self) -> List['IntrfcFrame']:
        return self.Interfaces
    
    def DevAddIntrfc(self, iff: 'IntrfcFrame') -> Optional[Exception]:
        return self.AddIntrfc(iff)
    
    # AddIntrfc includes a new interface frame for the endpt.
    # An error is reported if this specific (by pointer value or by name) interface is already connected.
    def AddIntrfc(self, iff: 'IntrfcFrame') -> Optional[Exception]:
        for ih in self.Interfaces:
            if ih == iff or ih.Name == iff.Name:
                return Exception(f"attempt to re-add interface {iff.Name} to switch {self.Name}")
        
        # ensure that interface states its presence on this device
        iff.DevType = "Endpt"
        iff.Device = self.Name
        
        # save the interface
        self.Interfaces.append(iff)
        return None
    
    # Transform returns a serializable EndptDesc, transformed from a EndptFrame.
    def Transform(self) -> 'EndptDesc':
        ed = EndptDesc()
        ed.Name = self.Name
        ed.Model = self.Model
        ed.Groups = copy.deepcopy(self.Groups)
        ed.Cores = self.Cores
        ed.Accel = copy.deepcopy(self.Accel)
        ed.Interfaces = [iface.Transform() for iface in self.Interfaces]
        return ed

    # AddAccel includes the name of an accelerator PIC in the Endpoint, including a core
    # count if greater than 1
    def AddAccel(self, name: str, model: str, cores: int):
        accelName = name
        if cores > 1:
            accelName += f",{cores}"
        self.Accel[accelName] = model

    # SetEUD includes EUD into the group list
    def SetEUD(self):
        self.AddGroup("EUD")

    # IsEUD indicates whether EUD is in the group list
    def IsEUD(self) -> bool:
        return "EUD" in self.Groups

    # SetHost includes Host into the group list
    def SetHost(self):
        self.AddGroup("Host")

    # IsHost indicates whether Host is in the group list
    def IsHost(self) -> bool:
        return "Host" in self.Groups

    # SetSrvr includes Server into the group list
    def SetSrvr(self):
        self.AddGroup("Server")

    # IsSrvr indicates whether Server is in the group list
    def IsSrvr(self) -> bool:
        return "Server" in self.Groups

    # SetCores sets the number of cores for the endpoint
    def SetCores(self, cores: int):
        self.Cores = cores

    # AddGroup adds a group name to an endpoint frame's list of groups, if not already present
    def AddGroup(self, groupName: str):
        if groupName not in self.Groups:
            self.Groups.append(groupName)

# DefaultEndptName returns unique name for a endpt
def DefaultEndptName(etype: str) -> str:
    global numberOfEndpts
    return f"{etype}-endpt.({numberOfEndpts})"

# CreateEndpt is a constructor. It saves (or creates) the endpt name, and saves
# the optional endpt type (which has use in run-time configuration)
def CreateEndpt(name: str, etype: str, model: str, cores: int) -> 'EndptFrame':
    global numberOfEndpts, objTypeByName, devByName
    epf = EndptFrame()
    numberOfEndpts += 1
    
    epf.Model = model
    epf.Cores = cores
    epf.Accel = {}
    
    if len(name) == 0:
        epf.Name = DefaultEndptName(etype)
    else:
        epf.Name = name

    objTypeByName[epf.Name] = "Endpt"
    devByName[epf.Name] = epf
    
    epf.Interfaces = []
    epf.Groups = []
    
    return epf

# CreateHost is a constructor.  It creates an endpoint frame that sets the Host flag
def CreateHost(name: str, model: str, cores: int) -> 'EndptFrame':
    host = CreateEndpt(name, "Host", model, cores)
    host.AddGroup("Host")
    return host

# CreateNode is a constructor.  It creates an endpoint frame, does not mark with Host, Server, or EUD
def CreateNode(name: str, model: str, cores: int) -> 'EndptFrame':
    return CreateEndpt(name, "Node", model, cores)

# CreateSensor is a constructor.
def CreateSensor(name: str, model: str) -> 'EndptFrame':
    sensor = CreateEndpt(name, "Sensor", model, 1)
    sensor.AddGroup("Sensor")
    return sensor

# CreateSrvr is a constructor.  It creates an endpoint frame and marks it as a server
def CreateSrvr(name: str, model: str, cores: int) -> 'EndptFrame':
    endpt = CreateEndpt(name, "Srvr", model, cores)
    endpt.AddGroup("Server")
    return endpt

# CreateEUD is a constructor.  It creates an endpoint frame with the EUD flag set to true
def CreateEUD(name: str, model: str, cores: int) -> 'EndptFrame':
    epf = CreateEndpt(name, "EUD", model, cores)
    epf.AddGroup("EUD")
    return epf

# endptPresent returns a boolean flag indicating whether an endpoint frame given as an argument
# exists already in a list of endpoint frames given as another argument
def endptPresent(endptList: List['EndptFrame'], endpt: 'EndptFrame') -> bool:
    for endptInList in endptList:
        if endptInList.Name == endpt.Name:
            return True
    return False

# ConnectNetworks creates router that enables traffic to pass between
# the two argument networks. 'newRtr' input variable governs whether
# a new router is absolutely created (allowing for multiple connections),
# or only if there is no existing connection
def ConnectNetworks(net1, net2, newRtr: bool):
    # count the number of routers that net1 and net2 share already
    shared = 0
    for rtr1 in net1.Routers:
        for rtr2 in net2.Routers:
            if rtr1.Name == rtr2.Name:
                shared += 1

    # if one or more is shared already and newRtr is false, just return nil
    if shared > 0 and not newRtr:
        return None, None

    # create a router that has one interface towards net1 and the other towards net2
    name = f"Rtr:({net1.Name}-{net2.Name})"
    if net2.Name < net1.Name:
        name = f"Rtr:({net2.Name}-{net1.Name})"

    # append shared+1 to ensure no duplication in router names
    name = f"{name}.[{shared+1}]"
    rtr = CreateRouter(name, "")

    # create an interface bound to rtr that faces net1
    intrfc1 = CreateIntrfc(rtr.Name, "", "Router", net1.MediaType, net1.Name)
    intrfc1Err = rtr.AddIntrfc(intrfc1)

    # create an interface bound to rtr that faces net2
    intrfc2 = CreateIntrfc(rtr.Name, "", "Router", net2.MediaType, net2.Name)
    intrfc2Err = rtr.AddIntrfc(intrfc2)

    return rtr, ReportErrs([intrfc1Err, intrfc2Err])

# The TopoCfgFrame struct gives the highest level structure of the topology,
# is ultimately the encompassing dictionary in the serialization
class TopoCfgFrame:
    def __init__(self, Name: str):
        self.Name: str = Name
        self.Endpts: List['EndptFrame'] = []
        self.Networks: List['NetworkFrame'] = []
        self.Routers: List['RouterFrame'] = []
        self.Switches: List['SwitchFrame'] = []
        InitTopoDesc()

    # addEndpt adds a Endpt to the topology configuration (if it is not already present).
    # Does not create an interface
    def addEndpt(self, endpt: 'EndptFrame'):
        # test for duplication either by address or name
        inputName = endpt.Name
        for stored in self.Endpts:
            if endpt == stored or inputName == stored.Name:
                return
        
        # add it
        self.Endpts.append(endpt)

    # AddNetwork adds a Network to the topology configuration (if it is not already present).
    def AddNetwork(self, net: 'NetworkFrame'):
        inputName = net.Name
        for stored in self.Networks:
            if net == stored or inputName == stored.Name:
                return
            
        self.Networks.append(net)

    # addRouter adds a Router to the topology configuration (if it is not already present).
    def addRouter(self, rtr: 'RouterFrame'):
        inputName = rtr.Name
        for stored in self.Routers:
            if rtr == stored or inputName == stored.Name:
                return
        self.Routers.append(rtr)

    # addSwitch adds a Switch to the topology configuration (if it is not already present).
    def addSwitch(self, swtch: 'SwitchFrame'):
        inputName = swtch.Name
        for stored in self.Switches:
            if swtch == stored or inputName == stored.Name:
                return
        self.Switches.append(swtch)

    # Consolidate gathers endpts, switches, and routers from the networks added to the TopoCfgFrame,
    # and make sure that all the devices referred to in the different components are exposed
    # at the TopoCfgFrame level
    def Consolidate(self) -> Optional[Exception]:
        if len(self.Networks) == 0:
            return Exception("no networks given in TopoCfgFrame in Consolidate call")
        
        self.Endpts = []
        self.Routers = []
        self.Switches = []

        for net in self.Networks:
            for rtr in net.Routers:
                self.addRouter(rtr)
            for endpt in net.Endpts:
                self.addEndpt(endpt)
            for swtch in net.Switches:
                self.addSwitch(swtch)

        return None

    # Transform transforms the slices of pointers to network objects
    # into slices of instances of those objects, for serialization
    def Transform(self) -> 'TopoCfg':
        # ensure we're consolidated
        cerr = self.Consolidate()
        if cerr is not None:
            raise Exception(cerr)
        
        # create the TopoCfg
        TD = TopoCfg(self.Name)
        TD.Name = self.Name

        TD.Endpts = [endptf.Transform() for endptf in self.Endpts]
        TD.Networks = [netf.Transform() for netf in self.Networks]
        TD.Routers = [rtrf.Transform() for rtrf in self.Routers]
        TD.Switches = [switchf.Transform() for switchf in self.Switches]

        return TD

# CreateTopoCfgFrame is a constructor for TopoCfgFrame
def CreateTopoCfgFrame(name: str) -> 'TopoCfgFrame':
    return TopoCfgFrame(name)

RtrDescSlice = List['RouterDesc']
EndptDescSlice = List['EndptDesc']
NetworkDescSlice = List['NetworkDesc']
SwitchDescSlice = List['SwitchDesc']
FlowDescSlice = List['FlowDesc']

# TopoCfg contains all of the networks, routers, and
# endpts, as they are listed in the json file.
class TopoCfg:
    def __init__(self, Name: str = ""):
        self.Name: str = Name
        self.Networks: NetworkDescSlice = []
        self.Routers: RtrDescSlice = []
        self.Endpts: EndptDescSlice = []
        self.Switches: SwitchDescSlice = []
        self.Flows: FlowDescSlice = []

    # WriteToFile serializes the TopoCfg and writes to the file whose name is given as an input argument.
    # Extension of the file name selects whether serialization is to json or to yaml format.
    def WriteTopoCfgToFile(self, filename: str) -> Optional[Exception]:
        pathExt = os.path.splitext(filename)[1]
        try:
            if pathExt in [".yaml", ".YAML", ".yml"]:
                with open(filename, "w") as f:
                    yaml.dump(self, f, default_flow_style=False)
            elif pathExt in [".json", ".JSON"]:
                with open(filename, "w") as f:
                    json.dump(self, f, default=lambda o: o.__dict__, indent=4)
            else:
                return Exception("Unsupported file extension")
        except Exception as e:
            return e
        
        return None

# A TopoCfgDict holds instances of TopoCfg structures, in a map whose key is
# a name for the topology.  Used to store pre-built instances of networks
class TopoCfgDict:
    def __init__(self, DictName: str = ""):
        self.DictName: str = DictName
        self.Cfgs: Dict[str, TopoCfg] = {}

    # AddTopoCfg includes a TopoCfg into the dictionary, optionally returning an error
    # if an TopoCfg with the same name has already been included
    def AddTopoCfg(self, tc: TopoCfg, overwrite: bool) -> Optional[Exception]:
        if not overwrite and tc.Name in self.Cfgs:
            return Exception(f"attempt to overwrite TopoCfg {tc.Name} in TopoCfgDict")
        
        self.Cfgs[tc.Name] = tc
        return None

    # RecoverTopoCfg returns a copy (if one exists) of the TopoCfg with name equal to the input argument name.
    # Returns a boolean indicating whether the entry was actually found
    def RecoverTopoCfg(self, name: str) -> Optional[TopoCfg]:
        return self.Cfgs.get(name, None)

    # WriteToFile serializes the TopoCfgDict and writes to the file whose name is given as an input argument.
    # Extension of the file name selects whether serialization is to json or to yaml format.
    def WriteToFile(self, filename: str) -> Optional[Exception]:
        pathExt = os.path.splitext(filename)[1]
        try:
            if pathExt in [".yaml", ".YAML", ".yml"]:
                with open(filename, "w") as f:
                    yaml.dump(self, f, default_flow_style=False)
            elif pathExt in [".json", ".JSON"]:
                with open(filename, "w") as f:
                    json.dump(self, f, default=lambda o: o.__dict__, indent=4)
            else:
                return Exception("Unsupported file extension")
        except Exception as e:
            return e
        
        return None

# ReadTopoCfgDict deserializes a slice of bytes into a TopoCfgDict.  If the input arg of bytes
# is empty, the file whose name is given as an argument is read.  Error returned if
# any part of the process generates the error.
def ReadTopoCfgDict(filename: str, useYAML: bool, dict_bytes: Optional[bytes] = None) -> Optional['TopoCfgDict']:
    if not dict_bytes:
        with open(filename, "rb") as f:
            dict_bytes = f.read()
    if useYAML:
        data = yaml.safe_load(dict_bytes)
    else:
        data = json.loads(dict_bytes)

    tcd = TopoCfgDict(data.get('DictName', ''))
    tcd.Cfgs = {k: TopoCfg(**v) for k, v in data.get('Cfgs', {}).items()}
    
    return tcd
# TODO: pretty sure this doesn't work, but need to double check

# CreateTopoCfgDict is a constructor. Saves the dictionary name, initializes the TopoCfg map.
def CreateTopoCfgDict(name: str) -> 'TopoCfgDict':
    return TopoCfgDict(name)

# ReadTopoCfg deserializes a slice of bytes into a TopoCfg.  If the input arg of bytes
# is empty, the file whose name is given as an argument is read.  Error returned if
# any part of the process generates the error.
def ReadTopoCfg(filename: str, useYAML: bool, dict_bytes: Optional[bytes] = None) -> Optional[TopoCfg]:
    if not dict_bytes:
        with open(filename, "rb") as f:
            dict_bytes = f.read()
    if useYAML:
        data = yaml.safe_load(dict_bytes)
    else:
        data = json.loads(dict_bytes)

    return TopoCfg(**data)
# TODO: potential fix: https://pypi.org/project/pymarshal/


# DevDesc provides a description of a computing (or switching or routing) device
class DevDesc:
    def __init__(self, DevTypes: List[str], Manufacturer: str, Model: str, Cores: int, Freq: float, Cache: float):
        self.DevTypes: List[str] = DevTypes
        self.Manufacturer: str = Manufacturer
        self.Model: str = Model
        self.Cores: int = Cores
        self.Freq: float = Freq
        self.Cache: float = Cache


# DevDescDict defines a dictionary of DevDesc structs, indexed by device description string
class DevDescDict:
    def __init__(self, Name: str = ""):
        self.Name: str = Name
        self.DescMap: Dict[str, DevDesc] = {}

    # AddDevDesc constructs a device identifier by concatenating the Manufacturer and Model
    # attributes of the argument device as the index to the referring DevDescDict
    def AddDevDesc(self, dd: DevDesc):
        name = f"{dd.Manufacturer}-{dd.Model}"
        self.DescMap[name] = dd

    # WriteToFile stores the DevDescDict struct to the file whose name is given.
    # Serialization to json or to yaml is selected based on the extension of this name.
    def WriteToFile(self, filename: str) -> Optional[Exception]:
        pathExt = os.path.splitext(filename)[1]
        try:
            if pathExt in [".yaml", ".YAML", ".yml"]:
                with open(filename, "w") as f:
                    yaml.dump(self, f, default_flow_style=False)
            elif pathExt in [".json", ".JSON"]:
                with open(filename, "w") as f:
                    json.dump(self, f, default=lambda o: o.__dict__, indent=4)
            else:
                return Exception("Unsupported file extension")
        except Exception as e:
            return e
        return None

    def RecoverDevDesc(self, name: str) -> DevDesc:
        # RecoverDevDesc returns a DevDesc from the dictionary, or raises if not found
        if name not in self.DescMap:
            raise Exception(f"device description map {name} not found")
        return self.DescMap[name]

# CreateDevDescDict is a constructor
def CreateDevDescDict(name: str) -> 'DevDescDict':
    return DevDescDict(name)

# CreateDevDesc is a constructor
def CreateDevDesc(devType: str, manufacturer: str, model: str, cores: int, freq: float, cache: float) -> DevDesc:
    devTypes = devType.split(":")
    return DevDesc(devTypes, manufacturer, model, cores, freq, cache)

# ReadDevDescDict deserializes a byte slice holding a representation of an DevDescDict struct.
# If the input argument of dict (those bytes) is empty, the file whose name is given is read
# to acquire them.  A deserialized representation is returned, or an error if one is generated
# from a file read or the deserialization.
def ReadDevDescDict(filename: str, useYAML: bool, dict_bytes: Optional[bytes] = None) -> Optional['DevDescDict']:
    if not dict_bytes:
        with open(filename, "rb") as f:
            dict_bytes = f.read()
    if useYAML:
        data = yaml.safe_load(dict_bytes)
    else:
        data = json.loads(dict_bytes)
    ddd = DevDescDict(data.get('Name', ''))
    ddd.DescMap = {k: DevDesc(**v) for k, v in data.get('DescMap', {}).items()}
    return ddd

# ReportErrs transforms a list of errors and transforms the non-nil ones into a single error
# with comma-separated report of all the constituent errors, and returns it.
def ReportErrs(errs: List[Optional[Exception]]) -> Optional[Exception]:
    errMsg = [str(err) for err in errs if err is not None]
    if not errMsg:
        return None
    return Exception(",".join(errMsg))

# CheckDirectories probes the file system for the existence
# of every directory listed in the list of files.  Returns a boolean
# indicating whether all dirs are valid, and returns an aggregated error
# if any checks failed.
def CheckDirectories(dirs: List[str]) -> tuple[bool, Optional[Exception]]:
    # make sure that every directory name included exists
    failures = []

    # for every offered (non-empty) directory
    for dir in dirs:
        if not dir:
            continue
        
        # look for extension; if found, not a directory
        ext = os.path.splitext(dir)[1]
        if ext:
            failures.append(f"{dir} not a directory")
            continue

        if not os.path.isdir(dir):
            failures.append(f"{dir} not reachable")
            continue

    if not failures:
        return True, None
    
    return False, Exception(",".join(failures))

# CheckFiles probes the file system for permitted access to all the
# argument filenames, optionally checking also for the existence
# of those files for the purposes of reading them.
def CheckFiles(names: List[str], checkExistence: bool) -> tuple[bool, Optional[Exception]]:
    # make sure that every directory for every file exists
    errs = []

    for name in names:
        # skip non-existent files
        if not name or name == "/tmp":
            continue

        # split off the directory portion of the path
        directory = os.path.dirname(name)
        if not os.path.isdir(directory):
            errs.append(Exception(f"Directory {directory} not found"))
    
    # if required, check for the reachability and existence of every file
    if checkExistence:
        for name in names:
            if not os.path.isfile(name):
                errs.append(Exception(f"File {name} not found"))
        
        if not errs:
            return True, None
        return False, ReportErrs(errs)
    
    return True, None

# CheckReadableFiles probles the file system to ensure that every
# one of the argument filenames exists and is readable
def CheckReadableFiles(names: List[str]) -> tuple[bool, Optional[Exception]]:
    return CheckFiles(names, True)

# CheckOutputFiles probles the file system to ensure that every
# argument filename can be written.
def CheckOutputFiles(names: List[str]) -> tuple[bool, Optional[Exception]]:
    return CheckFiles(names, False)
