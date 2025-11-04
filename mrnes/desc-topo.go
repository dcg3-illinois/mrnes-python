package mrnes

// file desc-topo.go holds structs, methods, and data structures supporting
// the construction of and access to models of computers and networks using
// the mrnes API

import (
	"encoding/json"
	"errors"
	"fmt"
	"golang.org/x/exp/slices"
	"gopkg.in/yaml.v3"
	"os"
	"path"
	"path/filepath"
	"strconv"
	"strings"
)

// A DevExecDesc struct holds a description of a device operation timing.
// ExecTime is the time (in seconds), it depends on attribute Model
type DevExecDesc struct {
	DevOp    string  `json:"devop" yaml:"devop"`
	Model    string  `json:"model" yaml:"model"`
	PcktLen  int     `json:"pcktlen" yaml:"pcktlen"`
	ExecTime float64 `json:"exectime" yaml:"exectime"`
	Bndwdth  float64 `json:"bndwdth" yaml:"bndwdth"`
}

// A DevExecList holds a map (Times) whose key is the operation
// of a device, and whose value is a list of DevExecDescs
// associated with that operation.
type DevExecList struct {
	// ListName is an identifier for this collection of timings
	ListName string `json:"listname" yaml:"listname"`

	// key is the device operation.  Each has a list
	// of descriptions of the timing of that operation, as a function of device model
	Times map[string][]DevExecDesc `json:"times" yaml:"times"`
}

// CreateDevExecList is an initialization constructor.
// Its output struct has methods for integrating data.
func CreateDevExecList(listname string) *DevExecList {
	del := new(DevExecList)
	del.ListName = listname
	del.Times = make(map[string][]DevExecDesc)

	return del
}

// WriteToFile stores the DevExecList struct to the file whose name is given.
// Serialization to json or to yaml is selected based on the extension of this name.
func (del *DevExecList) WriteToFile(filename string) error {
	pathExt := path.Ext(filename)
	var bytes []byte
	var merr error = nil

	if pathExt == ".yaml" || pathExt == ".YAML" || pathExt == ".yml" {
		bytes, merr = yaml.Marshal(*del)
	} else if pathExt == ".json" || pathExt == ".JSON" {
		bytes, merr = json.MarshalIndent(*del, "", "\t")
	}

	if merr != nil {
		panic(merr)
	}

	f, cerr := os.Create(filename)
	if cerr != nil {
		panic(cerr)
	}
	_, werr := f.WriteString(string(bytes[:]))
	if werr != nil {
		panic(werr)
	}
	err := f.Close()
	if err != nil {
		panic(err)
	}

	return werr
}

// ReadDevExecList deserializes a byte slice holding a representation of an DevExecList struct.
// If the input argument of dict (those bytes) is empty, the file whose name is given is read
// to acquire them.  A deserialized representation is returned, or an error if one is generated
// from a file read or the deserialization.
func ReadDevExecList(filename string, useYAML bool, dict []byte) (*DevExecList, error) {
	var err error

	// if the dict slice of bytes is empty we get them from the file whose name is an argument
	if len(dict) == 0 {
		dict, err = os.ReadFile(filename)
		if err != nil {
			return nil, err
		}
	}

	example := DevExecList{}

	if useYAML {
		err = yaml.Unmarshal(dict, &example)
	} else {
		err = json.Unmarshal(dict, &example)
	}

	if err != nil {
		return nil, err
	}

	return &example, nil
}

// AddTiming takes the parameters of a DevExecDesc, creates one, and adds it to the FuncExecList
func (del *DevExecList) AddTiming(devOp, model string, execTime float64, pcktlen int, bndwdth float64) {
	_, present := del.Times[devOp]
	if !present {
		del.Times[devOp] = make([]DevExecDesc, 0)
	}
	del.Times[devOp] = append(del.Times[devOp],
		DevExecDesc{Model: model, DevOp: devOp, ExecTime: execTime, PcktLen: pcktlen, Bndwdth: bndwdth})
}

// numberOfIntrfcs (and more generally, numberOf{Objects}
// are counters of the number of default instances of each
// object type have been created, and so can be used
// to help create unique default names for these objects
//
//	Not currently used at initialization, see if useful for the simulation
var numberOfIntrfcs int
var numberOfRouters int
var numberOfSwitches int
var numberOfEndpts int
var numberOfFlows int

// maps that let you use a name to look up an object
var objTypeByName map[string]string
var devByName map[string]NetDevice
var netByName map[string]*NetworkFrame
var rtrByName map[string]*RouterFrame
var flowByName map[string]*FlowFrame

// devConnected gives for each NetDev device a list of the other NetDev devices
// it connects to through wired interfaces
var devConnected map[string][]string

func InitTopoDesc() {
	numberOfIntrfcs = 0
	numberOfRouters = 0
	numberOfSwitches = 0
	numberOfEndpts = 0
	numberOfFlows = 0

	// maps that let you use a name to look up an object
	objTypeByName = make(map[string]string)
	devByName = make(map[string]NetDevice)
	netByName = make(map[string]*NetworkFrame)
	rtrByName = make(map[string]*RouterFrame)
	flowByName = make(map[string]*FlowFrame)

	// devConnected gives for each NetDev device a list of the other NetDev devices
	// it connects to through wired interfaces
	devConnected = make(map[string][]string)
}

// To most easily serialize and deserialize the various structs involved in creating
// and communicating a simulation model, we ensure that they are all completely
// described without pointers, every structure is fully instantiated in the description.
// On the other hand it is easily to manage the construction of complicated structures
// under the rules Golang uses for memory management if we allow pointers.
// Our approach then is to define two respresentations for each kind of structure.  One
// has the final appellation of 'Frame', and holds pointers.  The pointer free version
// has the final  appellation of 'Desc'.   After completely building the structures using
// Frames we transform each into a Desc version for serialization.

// The NetDevice interface lets us use common code when network objects
// (endpt, switch, router, network, flow) are involved in model construction.
type NetDevice interface {
	DevName() string                 // returns the .Name field of the struct
	DevID() string                   // returns a unique (string) identifier for the struct
	DevType() string                 // returns the type ("Switch","Router","Endpt","Network")
	DevInterfaces() []*IntrfcFrame   // list of interfaces attached to the NetDevice, if any
	DevAddIntrfc(*IntrfcFrame) error // function to add another interface to the netDevic3
}

// IntrfcDesc defines a serializable description of a network interface
type IntrfcDesc struct {
	// name for interface, unique among interfaces on endpting device.
	Name string `json:"name" yaml:"name"`

	Groups []string `json:"groups" yaml:"groups"`

	// type of device that is home to this interface, i.e., "Endpt", "Switch", "Router"
	DevType string `json:"devtype" yaml:"devtype"`

	// whether media used by interface is 'wired' or 'wireless' .... could put other kinds here, e.g., short-wave, satellite
	MediaType string `json:"mediatype" yaml:"mediatype"`

	// name of endpt, switch, or router on which this interface is resident
	Device string `json:"device" yaml:"device"`

	// name of interface (on a different device) to which this interface is directly (and singularly) connected
	Cable string `json:"cable" yaml:"cable"`

	// name of interface (on a different device) to which this interface is directly (and singularly) carried if wired and not on Cable
	Carry []string `json:"carry" yaml:"carry"`

	// list of names of interface (on a different device) to which this interface is connected through wireless
	Wireless []string `json:"wireless" yaml:"wireless"`

	// name of the network the interface connects to. There is a tacit assumption then that interface reaches routers on the network
	Faces string `json:"faces" yaml:"faces"`
}

// IntrfcFrame gives a pre-serializable description of an interface, used in model construction.
// 'Almost' the same as IntrfcDesc, with the exception of one pointer
type IntrfcFrame struct {
	// name for interface, unique among interfaces on endpting device.
	Name string

	Groups []string

	// type of device that is home to this interface, i.e., "Endpt", "Switch", "Router"
	DevType string

	// whether media used by interface is 'wired' or 'wireless' .... could put other kinds here, e.g., short-wave, satellite
	MediaType string

	// name of endpt, switch, or router on which this interface is resident
	Device string

	// pointer to interface (on a different device) to which this interface is directly (and singularly) connected.
	// this interface and the one pointed to need to have media type "wired"
	Cable *IntrfcFrame

	// pointer to interface (on a different device) to which this interface is directly if wired and not Cable.
	// this interface and the one pointed to need to have media type "wired", and have "Cable" be empty
	Carry []*IntrfcFrame

	// A wireless interface may connect to may devices, this slice points to those that can be reached
	Wireless []*IntrfcFrame

	// name of the network the interface connects to. We do not require that the media type of the interface be the same
	// as the media type of the network.
	Faces string
}

// DefaultIntrfcName generates a unique string to use as a name for an interface.
// That name includes the name of the device endpting the interface and a counter
func DefaultIntrfcName(device string) string {
	return fmt.Sprintf("intrfc@%s[.%d]", device, numberOfIntrfcs)
}

// CableIntrfcFrames links two interfaces through their 'Cable' attributes
func CableIntrfcFrames(intrfc1, intrfc2 *IntrfcFrame) {
	intrfc1.Cable = intrfc2
	intrfc2.Cable = intrfc1
}

// CarryIntrfcFrames links two interfaces through their 'Cable' attributes
func CarryIntrfcFrames(intrfc1, intrfc2 *IntrfcFrame) {
	found := false
	for _, carry := range intrfc1.Carry {
		if carry.Name == intrfc2.Name {
			found = true
			break
		}
	}
	if !found {
		intrfc1.Carry = append(intrfc1.Carry, intrfc2)
	}

	found = false
	for _, carry := range intrfc2.Carry {
		if carry.Name == intrfc1.Name {
			found = true
			break
		}
	}
	if !found {
		intrfc2.Carry = append(intrfc2.Carry, intrfc1)
	}
}

// CreateIntrfc is a constructor for [IntrfcFrame] that fills in most of the attributes except Cable.
// Arguments name the device holding the interface and its type, the type of communication fabric the interface uses, and the
// name of the network the interface connects to
func CreateIntrfc(device, name, devType, mediaType, faces string) *IntrfcFrame {
	intrfc := new(IntrfcFrame)

	// counter used in the generation of default names
	numberOfIntrfcs += 1

	// an empty string given as name flags that we should create a default one.
	if len(name) == 0 {
		name = DefaultIntrfcName(device)
	}

	// fill in structure attributes included in function call
	intrfc.Device = device
	intrfc.Name = name
	intrfc.DevType = devType
	intrfc.MediaType = mediaType
	intrfc.Faces = faces
	intrfc.Wireless = make([]*IntrfcFrame, 0)
	intrfc.Groups = []string{}

	// if the device in which this interface is embedded is not a router we are done
	if devType != "Router" {
		return intrfc
	}

	// embedded in a router. Get its frame and that of the network which is faced
	rtr := devByName[device].(*RouterFrame)
	net := netByName[faces]

	// before adding the router to the network's list of routers, check for duplication
	// (based on the router's name)
	duplicated := false
	for _, stored := range net.Routers {
		if rtr.Name == stored.Name {
			duplicated = true

			break
		}
	}

	// OK to save
	if !duplicated {
		net.Routers = append(net.Routers, rtr)
	}

	return intrfc
}

func (ifcf *IntrfcFrame) AddGroup(groupName string) {
	if !slices.Contains(ifcf.Groups, groupName) {
		ifcf.Groups = append(ifcf.Groups, groupName)
	}
}

// Transform converts an IntrfcFrame and returns an IntrfcDesc, for serialization.
func (ifcf *IntrfcFrame) Transform() IntrfcDesc {
	// most attributes are stright copies
	intrfcDesc := new(IntrfcDesc)
	intrfcDesc.Device = ifcf.Device
	intrfcDesc.Name = ifcf.Name
	intrfcDesc.DevType = ifcf.DevType
	intrfcDesc.MediaType = ifcf.MediaType
	intrfcDesc.Faces = ifcf.Faces
	intrfcDesc.Groups = ifcf.Groups
	intrfcDesc.Carry = make([]string, 0)
	intrfcDesc.Wireless = make([]string, 0)

	// a IntrfcDesc defines its Cable field to be a string, which
	// we set here to be the name of the interface the IntrfcFrame version
	// points to
	if ifcf.Cable != nil {
		intrfcDesc.Cable = ifcf.Cable.Name
	}

	// a IntrfcDesc defines its Carry field to be a string, which
	// we set here to be the name of the interface the IntrfcFrame version
	// points to
	for _, carry := range ifcf.Carry {
		intrfcDesc.Carry = append(intrfcDesc.Carry, carry.Name)
	}

	for _, connection := range ifcf.Wireless {
		intrfcDesc.Wireless = append(intrfcDesc.Wireless, connection.Name)
	}

	return *intrfcDesc
}

// isConnected is part of a set of functions and data structures useful in managing
// construction of a communication network. It indicates whether two devices whose
// identities are given are already connected through their interfaces, by Cable, Carry, or Wireless
func isConnected(id1, id2 string) bool {
	_, present := devConnected[id1]
	if !present {
		return false
	}
	for _, peerID := range devConnected[id1] {
		if peerID == id2 {
			return true
		}
	}

	return false
}

// MarkConnected modifes the devConnected data structure to reflect that
// the devices whose identities are the arguments have been connected.
func markConnected(id1, id2 string) {
	// if already connected there is nothing to do here
	if isConnected(id1, id2) {
		return
	}

	// for both devices, add their names to the 'connected to' list of the other

	// complete the data structure for devConnected[id1][id2] if need be
	_, present := devConnected[id1]
	if !present {
		devConnected[id1] = []string{}
	}

	devConnected[id1] = append(devConnected[id1], id2)

	// complete the data structure for devConnected[id2][id1] if need be
	_, present = devConnected[id2]
	if !present {
		devConnected[id2] = []string{}
	}
	devConnected[id2] = append(devConnected[id2], id1)
}

// determine whether intrfc1 is in the Carry slice of intrfc2
func carryContained(intrfc1, intrfc2 *IntrfcFrame) bool {
	for _, intrfc := range intrfc2.Carry {
		if intrfc == intrfc1 || intrfc.Name == intrfc1.Name {
			return true
		}
	}
	return false
}

// ConnectDevs establishes a 'cabled' or 'carry' connection (creating interfaces if needed) between
// devices dev1 and dev2 (recall that NetDevice is an interface satisified by Endpt, Router, Switch)
func ConnectDevs(dev1, dev2 NetDevice, cable bool, faces string) {
	// if already connected we don't do anything
	if isConnected(dev1.DevID(), dev2.DevID()) {
		return
	}

	// this call will record the connection
	markConnected(dev1.DevID(), dev2.DevID())

	// ensure that both devices are known to the network
	net := netByName[faces]
	err := net.IncludeDev(dev1, "wired", true)
	if err != nil {
		panic(err)
	}
	err = net.IncludeDev(dev2, "wired", true)
	if err != nil {
		panic(err)
	}

	// for each device collect all the interfaces that face the named network and are not wireless
	intrfcs1 := []*IntrfcFrame{}
	intrfcs2 := []*IntrfcFrame{}

	for _, intrfc := range dev2.DevInterfaces() {
		if intrfc.Faces == faces && !(intrfc.MediaType == "wireless") {
			intrfcs2 = append(intrfcs2, intrfc)
		}
	}

	for _, intrfc := range dev1.DevInterfaces() {
		if intrfc.Faces == faces && !(intrfc.MediaType == "wireless") {
			intrfcs1 = append(intrfcs1, intrfc)
		}
	}

	// check whether the connection requested exists already or we can complete it
	// without creating new interfaces

	if cable {
		for _, intrfc1 := range intrfcs1 {
			for _, intrfc2 := range intrfcs2 {

				// keep looping if we're looking for cable and they don't match
				if cable && intrfc1.Cable != nil && intrfc1.Cable != intrfc2 {
					continue
				}

				// either intrfc1.cable is nil or intrfc is connected already to intrfc2.
				// so then if intrfc2.cable is nil or is connected to intrfc1 we can complete the connection and leave
				if cable && (intrfc2.Cable == intrfc1 || intrfc2.Cable == nil) {
					intrfc1.Cable = intrfc2
					intrfc2.Cable = intrfc1
					return
				}
			}
		}
	} else {
		// see whether we can establish the connection without new interfaces
		for _, intrfc1 := range intrfcs1 {
			for _, intrfc2 := range intrfcs2 {
				if carryContained(intrfc1, intrfc2) && !carryContained(intrfc2, intrfc1) {
					// put intrfc2 in intrfc1's Carry slice
					intrfc2.Carry = append(intrfc2.Carry, intrfc1)
					return
				}
				if carryContained(intrfc2, intrfc1) && !carryContained(intrfc1, intrfc2) {
					// put intrfc1 in intrfc2's Carry slice
					intrfc1.Carry = append(intrfc1.Carry, intrfc1)
					return
				}
			}
		}
	}

	// no prior reason to complete connection between dev1 and dev2
	// see whether each has a 'free' interface, meaning it
	// points to the right network but is not yet cabled or carried
	var free1 *IntrfcFrame
	var free2 *IntrfcFrame

	// check dev1's interfaces
	for _, intrfc1 := range intrfcs1 {
		// we're looking to cable, the interface is facing the right network,
		// and it's cabling is empty
		if intrfc1.Faces == faces && intrfc1.Cable == nil {
			free1 = intrfc1
			break
		}
	}

	// if dev1 does not have a free interface, create one
	if free1 == nil {
		intrfcName := DefaultIntrfcName(dev1.DevName())
		free1 = CreateIntrfc(dev1.DevName(), intrfcName, dev1.DevType(), "wired", faces)
		err := dev1.DevAddIntrfc(free1)
		if err != nil {
			panic(err)
		}
	}

	// check dev2's interfaces
	for _, intrfc2 := range intrfcs2 {
		if intrfc2.Faces == faces && intrfc2.Cable == nil {
			free2 = intrfc2
			break
		}
	}

	// if dev2 does not have a free interface, create one
	if free2 == nil {
		intrfcName := DefaultIntrfcName(dev2.DevName())
		free2 = CreateIntrfc(dev2.DevName(), intrfcName, dev2.DevType(), "wired", faces)
		err := dev2.DevAddIntrfc(free2)
		if err != nil {
			panic(err)
		}
	}

	// found the interfaces, make the connection, using cable or carry as directed by the input argument
	if cable {
		free1.Cable = free2
		free2.Cable = free1
	} else {
		free1.Carry = append(free1.Carry, free2)
		free2.Carry = append(free2.Carry, free1)
	}
}

// WirelessConnectTo establishes a wireless connection (creating interfaces if needed)
// between a hub and a device
func (rf *RouterFrame) WirelessConnectTo(dev NetDevice, faces string) error {
	// ensure that both devices are known to the network
	net := netByName[faces]
	err := net.IncludeDev(rf, "wireless", true)
	if err != nil {
		return err
	}

	err = net.IncludeDev(dev, "wireless", true)
	if err != nil {
		return err
	}

	// ensure that hub has wireless interface facing the named network
	var hubIntrfc *IntrfcFrame
	for _, intrfc := range rf.Interfaces {
		if intrfc.MediaType == "wireless" && intrfc.Faces == faces {
			hubIntrfc = intrfc
			break
		}
	}

	// create an interface if necessary
	if hubIntrfc == nil {
		intrfcName := DefaultIntrfcName(rf.DevName())
		hubIntrfc = CreateIntrfc(rf.DevName(), intrfcName, rf.DevType(), "wireless", faces)
		err := rf.DevAddIntrfc(hubIntrfc)
		if err != nil {
			panic(err)
		}
	}

	// ensure that device has wireless interface facing the named network
	var devIntrfc *IntrfcFrame
	for _, intrfc := range dev.DevInterfaces() {
		if intrfc.MediaType == "wireless" && intrfc.Faces == faces {
			devIntrfc = intrfc
			break
		}
	}

	// create an interface if necessary
	if devIntrfc == nil {
		intrfcName := DefaultIntrfcName(dev.DevName())
		devIntrfc = CreateIntrfc(dev.DevName(), intrfcName, dev.DevType(), "wireless", faces)
		err := dev.DevAddIntrfc(devIntrfc)
		if err != nil {
			panic(err)
		}
	}

	hubIntrfcName := hubIntrfc.Name
	devIntrfcName := devIntrfc.Name

	// check whether dev is already known to hub
	devKnown := false
	for _, hconn := range hubIntrfc.Wireless {
		if hconn.Name == devIntrfcName {
			devKnown = true
			break
		}
	}

	// check whether hub is already known to dev
	hubKnown := false
	for _, dconn := range devIntrfc.Wireless {
		if dconn.Name == hubIntrfcName {
			hubKnown = true
			break
		}
	}

	// add as needed
	if !devKnown {
		hubIntrfc.Wireless = append(hubIntrfc.Wireless, devIntrfc)
	}
	if !hubKnown {
		devIntrfc.Wireless = append(devIntrfc.Wireless, hubIntrfc)
	}
	return nil
}

// A NetworkFrame holds the attributes of a network during the model construction phase
type NetworkFrame struct {
	// Name is a unique name across all objects in the simulation. It is used universally to reference this network
	Name string

	Groups []string

	// NetScale describes role of network, e.g., LAN, WAN, T3, T2, T1.  Used as an attribute when doing experimental configuration
	NetScale string

	// for now the network is either "wired" or "wireless"
	MediaType string

	// any router with an interface that faces this network is in this list
	Routers []*RouterFrame

	// any endpt with an interface that faces this network is in this list
	Endpts []*EndptFrame

	// any endpt with an interface that faces this network is in this list
	Switches []*SwitchFrame
}

// NetworkDesc is a serializable version of the Network information, where
// the pointers to routers, and switches are replaced by the string
// names of those entities
type NetworkDesc struct {
	Name      string   `json:"name" yaml:"name"`
	Groups    []string `json:"groups" yaml:"groups"`
	NetScale  string   `json:"netscale" yaml:"netscale"`
	MediaType string   `json:"mediatype" yaml:"mediatype"`
	Endpts    []string `json:"endpts" yaml:"endpts"`
	Routers   []string `json:"routers" yaml:"routers"`
	Switches  []string `json:"switches" yaml:"switches"`
}

// CreateNetwork is a constructor, with all the inherent attributes specified
func CreateNetwork(name, NetScale string, MediaType string) *NetworkFrame {
	nf := new(NetworkFrame)
	nf.Name = name           // name that is unique across entire simulation model
	nf.NetScale = NetScale   // type such as "LAN", "WAN", "T3", "T2", "T1"
	nf.MediaType = MediaType // currently "wired" or "wireless"

	// initialize slices
	nf.Routers = make([]*RouterFrame, 0)
	nf.Switches = make([]*SwitchFrame, 0)
	nf.Endpts = make([]*EndptFrame, 0)
	nf.Groups = []string{}

	objTypeByName[name] = "Network" // object name gets you object type
	netByName[name] = nf            // network name gets you network frame

	return nf
}

// FacedBy determines whether the device offered as an input argument
// has an interface whose 'Faces' component references this network
func (nf *NetworkFrame) FacedBy(dev NetDevice) bool {
	intrfcs := dev.DevInterfaces()
	netName := nf.Name
	for _, intrfc := range intrfcs {
		if intrfc.Faces == netName {
			return true
		}
	}
	return false
}

// AddGroup appends a group name to the network frame list of groups,
// checking first whether it is already present in the list
func (nf *NetworkFrame) AddGroup(groupName string) {
	if !slices.Contains(nf.Groups, groupName) {
		nf.Groups = append(nf.Groups, groupName)
	}
}

// DevNetworks returns a comma-separated string of
// of the names of networks the argument NetDevice interfaces
// face
func DevNetworks(dev NetDevice) string {
	nets := []string{}
	for _, intrfc := range dev.DevInterfaces() {
		nets = append(nets, intrfc.Faces)
	}
	return strings.Join(nets, ",")
}

// IncludeDev makes sure that the network device being offered
//
//	a) has an interface facing the network
//	b) is included in the network's list of those kind of devices
func (nf *NetworkFrame) IncludeDev(dev NetDevice, mediaType string, chkIntrfc bool) error {
	devName := dev.DevName()
	devType := dev.DevType()

	var intrfc *IntrfcFrame

	// check consistency of network media type and mediaType argument.
	// If mediaType is wireless then network must be wireless.   It is
	// permitted though to have the network be wireless and the mediaType be wired.
	if mediaType == "wireless" && !(nf.MediaType == "wireless") {
		return fmt.Errorf("including a wireless device in a wired network")
	}

	// if the device does not have an interface pointed at the network, make one
	if chkIntrfc && !nf.FacedBy(dev) {
		// create the interface
		intrfcName := DefaultIntrfcName(dev.DevName())
		intrfc = CreateIntrfc(devName, intrfcName, devType, mediaType, nf.Name)
	}

	// add it to the right list in the network
	switch devType {
	case "Endpt":
		endpt := dev.(*EndptFrame)
		if intrfc != nil {
			endpt.Interfaces = append(endpt.Interfaces, intrfc)
		}
		if !endptPresent(nf.Endpts, endpt) {
			nf.Endpts = append(nf.Endpts, endpt)
		}
	case "Router":
		rtr := dev.(*RouterFrame)
		if intrfc != nil {
			rtr.Interfaces = append(rtr.Interfaces, intrfc)
		}
		if !routerPresent(nf.Routers, rtr) {
			nf.Routers = append(nf.Routers, rtr)
		}

	case "Switch":
		swtch := dev.(*SwitchFrame)
		if intrfc != nil {
			swtch.Interfaces = append(swtch.Interfaces, intrfc)
		}
		if !switchPresent(nf.Switches, swtch) {
			nf.Switches = append(nf.Switches, swtch)
		}
	}
	return nil
}

// AddRouter includes the argument router into the network,
func (nf *NetworkFrame) AddRouter(rtrf *RouterFrame) error {
	// check whether a router with this same name already exists here
	for _, rtr := range nf.Routers {
		if rtr.Name == rtrf.Name {
			return nil
		}
	}
	if !nf.FacedBy(rtrf) {
		return fmt.Errorf("attempting to add router %s to network %s without prior association of an interface", rtrf.Name, nf.Name)
	}

	nf.Routers = append(nf.Routers, rtrf)
	return nil
}

// AddSwitch includes the argument router into the network,
// throws an error if already present
func (nf *NetworkFrame) AddSwitch(swtch *SwitchFrame) error {
	// check whether a router with this same name already exists here
	for _, nfswtch := range nf.Switches {
		if nfswtch.Name == swtch.Name {
			return nil
		}
	}
	if !nf.FacedBy(swtch) {
		return fmt.Errorf("attempting to add switch %s to network %s without prior association of an interface", swtch.Name, nf.Name)
	}

	nf.Switches = append(nf.Switches, swtch)
	return nil
}

// Transform converts a network frame into a network description.
// It copies string attributes, and converts pointers to routers, and switches
// to strings with the names of those entities
func (nf *NetworkFrame) Transform() NetworkDesc {
	nd := new(NetworkDesc)
	nd.Name = nf.Name
	nd.NetScale = nf.NetScale
	nd.MediaType = nf.MediaType
	nd.Groups = nf.Groups

	// in the frame the routers are pointers to objects, now we store their names
	nd.Routers = make([]string, len(nf.Routers))
	for idx := 0; idx < len(nf.Routers); idx += 1 {
		nd.Routers[idx] = nf.Routers[idx].Name
	}

	// in the frame the routers are pointers to objects, now we store their names
	nd.Endpts = make([]string, len(nf.Endpts))
	for idx := 0; idx < len(nf.Endpts); idx += 1 {
		nd.Endpts[idx] = nf.Endpts[idx].Name
	}

	// in the frame the routers are pointers to objects, now we store their names
	nd.Switches = make([]string, len(nf.Switches))
	for idx := 0; idx < len(nf.Switches); idx += 1 {
		nd.Switches[idx] = nf.Switches[idx].Name
	}

	return *nd
}

// RouterDesc describes parameters of a Router in the topology.
type RouterDesc struct {
	// Name is unique string identifier used to reference the router
	Name string `json:"name" yaml:"name"`

	Groups []string `json:"groups" yaml:"groups"`

	// Model is an attribute like "Cisco 6400". Used primarily in run-time configuration
	Model string `json:"model" yaml:"model"`

	OpDict	   map[string]string `json:"opdict" yaml:"opdict"`

	// list of names interfaces that describe the ports of the router
	Interfaces []IntrfcDesc `json:"interfaces" yaml:"interfaces"`
}

// RouterFrame describes parameters of a Router in the topology in pre-serialized form
type RouterFrame struct {
	Name       string // identical to RouterDesc attribute
	Groups     []string
	Model      string         // identifical to RouterDesc attribute
	OpDict	   map[string]string 
	Interfaces []*IntrfcFrame // list of interface frames that describe the ports of the router
}

// DefaultRouterName returns a unique name for a router
func DefaultRouterName() string {
	return fmt.Sprintf("rtr.[%d]", numberOfRouters)
}

// CreateRouter is a constructor, stores (possibly creates default) name, initializes slice of interface frames
func CreateRouter(name, model string) *RouterFrame {
	rtr := new(RouterFrame)
	numberOfRouters += 1

	rtr.Model = model

	if len(name) == 0 {
		name = DefaultRouterName()
	}

	rtr.Name = name
	objTypeByName[name] = "Router"
	devByName[name] = rtr
	rtrByName[name] = rtr
	rtr.Interfaces = make([]*IntrfcFrame, 0)
	rtr.Groups = []string{}
	return rtr
}

// routerPresent returns a boolean flag indicating whether
// a router provided as input exists already in a list of routers
// also provided as input
func routerPresent(rtrList []*RouterFrame, rtr *RouterFrame) bool {
	for _, rtrInList := range rtrList {
		if rtrInList.Name == rtr.Name {
			return true
		}
	}
	return false
}

// DevName returns the name of the NetDevice
func (rf *RouterFrame) DevName() string {
	return rf.Name
}

// DevType returns network objec type (e.g., "Switch", "Router", "Endpt", "Network") for the NetDevice
func (rf *RouterFrame) DevType() string {
	return "Router"
}

// DevID returns a unique identifier for the NetDevice
func (rf *RouterFrame) DevID() string {
	return rf.Name
}

// DevModel returns the NetDevice model code, if any
func (rf *RouterFrame) DevModel() string {
	return rf.Model
}

// DevInterfaces returns the slice of IntrfcFrame held by the NetDevice, if any
func (rf *RouterFrame) DevInterfaces() []*IntrfcFrame {
	return rf.Interfaces
}

// AddIntrfc includes interface frame in router frame
func (rf *RouterFrame) AddIntrfc(intrfc *IntrfcFrame) error {
	for _, ih := range rf.Interfaces {
		if ih == intrfc || ih.Name == intrfc.Name {
			return fmt.Errorf("attempt to re-add interface %s to switch %s", intrfc.Name, rf.Name)
		}
	}

	// ensure that the interface has stored the home device type and name
	intrfc.Device = rf.Name
	intrfc.DevType = "Router"
	rf.Interfaces = append(rf.Interfaces, intrfc)

	return nil
}

// DevAddIntrfc includes an IntrfcFrame to the NetDevice
func (rf *RouterFrame) DevAddIntrfc(iff *IntrfcFrame) error {
	return rf.AddIntrfc(iff)
}

// AddGroup includes a group name to the router
func (rf *RouterFrame) AddGroup(groupName string) {
	if !slices.Contains(rf.Groups, groupName) {
		rf.Groups = append(rf.Groups, groupName)
	}
}

// Transform returns a serializable RouterDesc, transformed from a RouterFrame.
func (rf *RouterFrame) Transform() RouterDesc {
	rd := new(RouterDesc)
	rd.Name = rf.Name
	rd.Model = rf.Model
	rd.Groups = rf.Groups
	rd.OpDict = rf.OpDict

	// create serializable representation of the interfaces by calling the Transform method on their Frame representation
	rd.Interfaces = make([]IntrfcDesc, len(rf.Interfaces))
	for idx := 0; idx < len(rf.Interfaces); idx += 1 {
		rd.Interfaces[idx] = rf.Interfaces[idx].Transform()
	}

	return *rd
}

// FlowFrame describes parameters of a Router in the topology in pre-serialized form
type FlowFrame struct {
	Name      string   `json:"name" yaml:"name"`
	SrcDev    string   `json:"srcdev"  yaml:"srcdev"`
	DstDev    string   `json:"dstdev"  yaml:"dstdev"`
	Mode      string   `json:"mode"    yaml:"mode"`
	ReqRate   float64  `json:"reqrate" yaml:"reqrate"`
	FrameSize int      `json:"framesize" yaml:"framesize"`
	Groups    []string `json:"groups" yaml:"groups"`
}

// DefaultFlowName returns a unique name for a flow
func DefaultFlowName() string {
	return fmt.Sprintf("flow.[%d]", numberOfFlows)
}

// CreateFlowFrame is a constructor, stores (possibly creates default) name
func CreateFlowFrame(name, srcDev, dstDev, mode string, reqRate float64, frameSize int) *FlowFrame {
	ff := new(FlowFrame)
	numberOfFlows += 1

	if len(name) == 0 {
		name = DefaultFlowName()
	}

	ff.Name = name
	ff.SrcDev = srcDev
	ff.DstDev = dstDev
	ff.Mode = mode
	ff.ReqRate = reqRate
	ff.FrameSize = frameSize

	objTypeByName[name] = "Flow"
	devByName[name] = ff
	flowByName[name] = ff
	ff.Groups = []string{}
	return ff
}

// DevName returns the name of the NetDevice
func (ff *FlowFrame) DevName() string {
	return ff.Name
}

// DevType returns network objec type (e.g., "Switch", "Router", "Endpt", "Network") for the NetDevice
func (ff *FlowFrame) DevType() string {
	return "Flow"
}

// DevID returns a unique identifier for the NetDevice
func (ff *FlowFrame) DevID() string {
	return ff.Name
}

// DevModel returns the NetDevice model code, if any
func (ff *FlowFrame) DevModel() string {
	return ""
}

// DevInterfaces returns the slice of IntrfcFrame held by the NetDevice, if any
func (ff *FlowFrame) DevInterfaces() []*IntrfcFrame {
	return []*IntrfcFrame{}
}

// DevAddIntrfc includes an IntrfcFrame to the NetDevice
func (ff *FlowFrame) DevAddIntrfc(iff *IntrfcFrame) error {
	return nil
}

// AddGroup includes a group name to the router
func (ff *FlowFrame) AddGroup(groupName string) {
	if !slices.Contains(ff.Groups, groupName) {
		ff.Groups = append(ff.Groups, groupName)
	}
}

// Transform returns a serializable RouterDesc, transformed from a FlowFrame.
func (ff *FlowFrame) Transform() FlowDesc {
	fd := new(FlowDesc)
	fd.Name = ff.Name
	fd.SrcDev = ff.SrcDev
	fd.DstDev = ff.DstDev
	fd.Mode = ff.Mode
	fd.ReqRate = ff.ReqRate
	fd.FrameSize = ff.FrameSize
	fd.Groups = ff.Groups
	return *fd
}

// FlowDesc describes parameters of a Router in the topology.
type FlowDesc struct {
	// Name is unique string identifier used to reference the flow
	Name      string   `json:"name" yaml:"name"`
	SrcDev    string   `json:"srcdev"  yaml:"srcdev"`
	DstDev    string   `json:"dstdev"  yaml:"dstdev"`
	Mode      string   `json:"mode"    yaml:"mode"`
	ReqRate   float64  `json:"reqrate" yaml:"reqrate"`
	FrameSize int      `json:"framesize" yaml:"framesize"`
	Groups    []string `json:"groups" yaml:"groups"`
}

// SwitchDesc holds a serializable representation of a switch.
type SwitchDesc struct {
	Name       string       `json:"name" yaml:"name"`
	Groups     []string     `json:"groups" yaml:"groups"`
	Model      string       `json:"model" yaml:"model"`
	OpDict	   map[string]string `json:"opdict" yaml:"opdict"`
	Interfaces []IntrfcDesc `json:"interfaces" yaml:"interfaces"`
}

// SwitchFrame holds a pre-serialization representation of a Switch
type SwitchFrame struct {
	Name       string // unique string identifier used to reference the router
	Groups     []string
	Model      string         // device model identifier
	OpDict	   map[string]string `json:"opdict" yaml:"opdict"`
	Interfaces []*IntrfcFrame // interface frames that describe the ports of the router
}

// DefaultSwitchName returns a unique name for a switch
func DefaultSwitchName(name string) string {
	return fmt.Sprintf("switch(%s).%d", name, numberOfSwitches)
}

// CreateSwitch constructs a switch frame.  Saves (and possibly creates) the switch name,
func CreateSwitch(name, model string) *SwitchFrame {
	sf := new(SwitchFrame)
	numberOfSwitches += 1

	if len(name) == 0 {
		name = DefaultSwitchName("switch")
	}
	objTypeByName[name] = "Switch" // from the name look up the type of object
	devByName[name] = sf           // from the name look up the device

	sf.Name = name
	sf.Model = model
	sf.Interfaces = make([]*IntrfcFrame, 0) // initialize for additions
	sf.Groups = []string{}

	return sf
}

// CreateHub constructs a switch frame tagged as being a hub
func CreateHub(name, model string) *SwitchFrame {
	hub := CreateSwitch(name, model)
	hub.AddGroup("Hub")
	return hub
}

// CreateBridge constructs a switch frame tagged as being a hub
func CreateBridge(name, model string) *SwitchFrame {
	bridge := CreateSwitch(name, model)
	bridge.AddGroup("Bridge")
	return bridge
}

// CreateRepeater constructs a switch frame tagged as being a repeater
func CreateRepeater(name, model string) *SwitchFrame {
	rptr := CreateSwitch(name, model)
	rptr.AddGroup("Repeater")
	return rptr
}

// AddIntrfc includes a new interface frame for the switch.  Error is returned
// if the interface (or one with the same name) is already attached to the SwitchFrame
func (sf *SwitchFrame) AddIntrfc(iff *IntrfcFrame) error {
	// check whether interface exists here already
	for _, ih := range sf.Interfaces {
		if ih == iff || ih.Name == iff.Name {
			return fmt.Errorf("attempt to re-add interface %s to switch %s", iff.Name, sf.Name)
		}
	}

	// ensure that the interface has stored the home device type and name
	iff.Device = sf.Name
	iff.DevType = "Switch"
	sf.Interfaces = append(sf.Interfaces, iff)

	return nil
}

// AddGroup adds a group to a switch's list of groups, if not already present in that list
func (sf *SwitchFrame) AddGroup(groupName string) {
	if !slices.Contains(sf.Groups, groupName) {
		sf.Groups = append(sf.Groups, groupName)
	}
}

// switchPresent returns a boolean flag indicating whether
// a switch provided as input exists already in a list of switches
// also provided as input
func switchPresent(swtchList []*SwitchFrame, swtch *SwitchFrame) bool {
	for _, swtchInList := range swtchList {
		if swtchInList.Name == swtch.Name {
			return true
		}
	}
	return false
}

// DevName returns name for the NetDevice
func (sf *SwitchFrame) DevName() string {
	return sf.Name
}

// DevType returns the type of the NetDevice (e.g. "Switch","Router","Endpt","Network")
func (sf *SwitchFrame) DevType() string {
	return "Switch"
}

// DevID returns unique identifier for NetDevice
func (sf *SwitchFrame) DevID() string {
	return sf.Name
}

// DevInterfaces returns list of IntrfcFrames attached to the NetDevice, if any
func (sf *SwitchFrame) DevInterfaces() []*IntrfcFrame {
	return sf.Interfaces
}

// DevAddIntrfc adds an IntrfcFrame to the NetDevice
func (sf *SwitchFrame) DevAddIntrfc(iff *IntrfcFrame) error {
	return sf.AddIntrfc(iff)
}

// Transform returns a serializable SwitchDesc, transformed from a SwitchFrame.
func (sf *SwitchFrame) Transform() SwitchDesc {
	sd := new(SwitchDesc)
	sd.Name = sf.Name
	sd.Model = sf.Model
	sd.Groups = sf.Groups
	sd.OpDict = sf.OpDict

	// serialize the interfaces by calling their own serialization routines
	sd.Interfaces = make([]IntrfcDesc, len(sf.Interfaces))
	for idx := 0; idx < len(sf.Interfaces); idx += 1 {
		sd.Interfaces[idx] = sf.Interfaces[idx].Transform()
	}

	return *sd
}

// EndptDesc defines serializable representation of an endpoint .
type EndptDesc struct {
	Name       string            `json:"name" yaml:"name"`
	Groups     []string          `json:"groups" yaml:"groups"`
	Model      string            `json:"model" yaml:"model"`
	Cores      int               `json:"cores" yaml:"cores"`
	Accel      map[string]string `json:"accel" yaml:"accel"`
	Interfaces []IntrfcDesc      `json:"interfaces" yaml:"interfaces"`
}

// EndptFrame defines pre-serialization representation of a Endpt
type EndptFrame struct {
	Name       string // unique string identifier
	Groups     []string
	Model      string
	Cores      int
	Accel      map[string]string
	Interfaces []*IntrfcFrame // list of interfaces that describe the networks the endpt connects to
}

// DefaultEndptName returns unique name for a endpt
func DefaultEndptName(etype string) string {
	return fmt.Sprintf("%s-endpt.(%d)", etype, numberOfEndpts)
}

// CreateHost is a constructor.  It creates an endpoint frame that sets the Host flag
func CreateHost(name, model string, cores int) *EndptFrame {
	host := CreateEndpt(name, "Host", model, cores)
	host.AddGroup("Host")
	return host
}

// CreateNode is a constructor.  It creates an endpoint frame, does not mark with Host, Server, or EUD
func CreateNode(name, model string, cores int) *EndptFrame {
	return CreateEndpt(name, "Node", model, cores)
}

// CreateSensor is a constructor.
func CreateSensor(name, model string) *EndptFrame {
	sensor := CreateEndpt(name, "Sensor", model, 1)
	sensor.AddGroup("Sensor")
	return sensor
}

// CreateSrvr is a constructor.  It creates an endpoint frame and marks it as a server
func CreateSrvr(name, model string, cores int) *EndptFrame {
	endpt := CreateEndpt(name, "Srvr", model, cores)
	endpt.AddGroup("Server")
	return endpt
}

// CreateEUD is a constructor.  It creates an endpoint frame with the EUD flag set to true
func CreateEUD(name, model string, cores int) *EndptFrame {
	epf := CreateEndpt(name, "EUD", model, cores)
	epf.AddGroup("EUD")
	return epf
}

// CreateEndpt is a constructor. It saves (or creates) the endpt name, and saves
// the optional endpt type (which has use in run-time configuration)
func CreateEndpt(name, etype string, model string, cores int) *EndptFrame {
	epf := new(EndptFrame)
	numberOfEndpts += 1

	epf.Model = model
	epf.Cores = cores

	epf.Accel = make(map[string]string)

	// get a (presumeably unique) string name
	if len(name) == 0 {
		name = DefaultEndptName(etype)
	}
	epf.Name = name
	objTypeByName[name] = "Endpt" // from name get type of object, here, "Endpt"
	devByName[name] = epf         // from name get object

	epf.Interfaces = make([]*IntrfcFrame, 0) // initialize slice of interface frames
	epf.Groups = []string{}

	return epf
}

// Transform returns a serializable EndptDesc, transformed from a EndptFrame.
func (epf *EndptFrame) Transform() EndptDesc {
	hd := new(EndptDesc)
	hd.Name = epf.Name
	hd.Model = epf.Model
	hd.Groups = make([]string, len(epf.Groups))
	copy(hd.Groups, epf.Groups)

	hd.Cores = epf.Cores
	hd.Accel = make(map[string]string)
	for key, value := range epf.Accel {
		hd.Accel[key] = value
	}

	// serialize the interfaces by calling the interface transformation function
	hd.Interfaces = make([]IntrfcDesc, len(epf.Interfaces))
	for idx := 0; idx < len(epf.Interfaces); idx += 1 {
		hd.Interfaces[idx] = epf.Interfaces[idx].Transform()
	}

	return *hd
}

// AddIntrfc includes a new interface frame for the endpt.
// An error is reported if this specific (by pointer value or by name) interface is already connected.
func (epf *EndptFrame) AddIntrfc(iff *IntrfcFrame) error {
	for _, ih := range epf.Interfaces {
		if ih == iff || ih.Name == iff.Name {
			return fmt.Errorf("attempt to re-add interface %s to switch %s", iff.Name, epf.Name)
		}
	}

	// ensure that interface states its presence on this device
	iff.DevType = "Endpt"
	iff.Device = epf.Name

	// save the interface
	epf.Interfaces = append(epf.Interfaces, iff)
	return nil
}

// AddAccel includes the name of an accelerator PIC in the Endpoint, including a core
// count if greater than 1
func (epf *EndptFrame) AddAccel(name string, model string, cores int) {
	accelName := name
	if cores > 1 {
		accelName += ("," + strconv.Itoa(cores))
	}
	epf.Accel[accelName] = model
}

// SetEUD includes EUD into the group list
func (epf *EndptFrame) SetEUD() {
	epf.AddGroup("EUD")
}

// IsEUD indicates whether EUD is in the group list
func (epf *EndptFrame) IsEUD() bool {
	return slices.Contains(epf.Groups, "EUD")
}

// SetHost includes Host into the group list
func (epf *EndptFrame) SetHost() {
	epf.AddGroup("Host")
}

// IsHost indicates whether Host is in the group list
func (epf *EndptFrame) IsHost() bool {
	return slices.Contains(epf.Groups, "Host")
}

// SetSrvr adds Server to the endpoint groups list
func (epf *EndptFrame) SetSrvr() {
	epf.AddGroup("Server")
}

// IsSrvr indicates whether Server is in the endpoint groups list
func (epf *EndptFrame) IsSrvr() bool {
	return slices.Contains(epf.Groups, "Server")
}

// SetCores records in the endpoint frame the number of cores the model assumes are available
// for concurrent processing
func (epf *EndptFrame) SetCores(cores int) {
	epf.Cores = cores
}

// AddGroup adds a group name to an endpoint frame's list of groups, if not already present
func (epf *EndptFrame) AddGroup(groupName string) {
	if !slices.Contains(epf.Groups, groupName) {
		epf.Groups = append(epf.Groups, groupName)
	}
}

// endptPresent returns a boolean flag indicating whether an endpoint frame given as an argument
// exists already in a list of endpoint frames given as another argument
func endptPresent(endptList []*EndptFrame, endpt *EndptFrame) bool {
	for _, endptInList := range endptList {
		if endptInList.Name == endpt.Name {
			return true
		}
	}
	return false
}

// DevName returns the NetDevice name
func (epf *EndptFrame) DevName() string {
	return epf.Name
}

// DevID returns the NetDevice unique identifier
func (epf *EndptFrame) DevID() string {
	return epf.Name
}

// DevType returns the NetDevice Type
func (epf *EndptFrame) DevType() string {
	return "Endpt"
}

// DevInterfaces returns the NetDevice list of IntrfcFrames, if any
func (epf *EndptFrame) DevInterfaces() []*IntrfcFrame {
	return epf.Interfaces
}

// DevAddIntrfc includes an IntrfcFrame to a NetDevice's list of IntrfcFrames
func (epf *EndptFrame) DevAddIntrfc(iff *IntrfcFrame) error {
	return epf.AddIntrfc(iff)
}

// ConnectNetworks creates router that enables traffic to pass between
// the two argument networks. 'newRtr' input variable governs whether
// a new router is absolutely created (allowing for multiple connections),
// or only if there is no existing connection
func ConnectNetworks(net1, net2 *NetworkFrame, newRtr bool) (*RouterFrame, error) {
	// count the number of routers that net1 and net2 share already
	shared := 0
	for _, rtr1 := range net1.Routers {
		for _, rtr2 := range net2.Routers {
			if rtr1.Name == rtr2.Name {
				shared += 1
			}
		}
	}

	// if one or more is shared already and newRtr is false, just return nil
	if shared > 0 && !newRtr {
		return nil, nil
	}

	// create a router that has one interface towards net1 and the other towards net1
	name := "Rtr:(" + net1.Name + "-" + net2.Name + ")"
	if net2.Name < net1.Name {
		name = "Rtr:(" + net2.Name + "-" + net1.Name + ")"
	}

	// append shared+1 to ensure no duplication in router names
	name = fmt.Sprintf("%s.[%d]", name, shared+1)
	rtr := CreateRouter(name, "")

	// create an interface bound to rtr that faces net1
	intrfc1 := CreateIntrfc(rtr.Name, "", "Router", net1.MediaType, net1.Name)
	intrfc1Err := rtr.AddIntrfc(intrfc1)

	// create an interface bound to rtr that faces net2
	intrfc2 := CreateIntrfc(rtr.Name, "", "Router", net2.MediaType, net2.Name)
	intrfc2Err := rtr.AddIntrfc(intrfc2)

	return rtr, ReportErrs([]error{intrfc1Err, intrfc2Err})
}

// The TopoCfgFrame struc gives the highest level structure of the topology,
// is ultimately the encompassing dictionary in the serialization
type TopoCfgFrame struct {
	Name     string
	Endpts   []*EndptFrame
	Networks []*NetworkFrame
	Routers  []*RouterFrame
	Switches []*SwitchFrame
}

// CreateTopoCfgFrame is a constructor.
func CreateTopoCfgFrame(name string) TopoCfgFrame {
	TF := new(TopoCfgFrame)
	TF.Name = name // save name

	// initialize all the TopoCfgFrame slices
	TF.Endpts = make([]*EndptFrame, 0)
	TF.Networks = make([]*NetworkFrame, 0)
	TF.Routers = make([]*RouterFrame, 0)
	TF.Switches = make([]*SwitchFrame, 0)
	InitTopoDesc()
	return *TF
}

// addEndpt adds a Endpt to the topology configuration (if it is not already present).
// Does not create an interface
func (tf *TopoCfgFrame) addEndpt(endpt *EndptFrame) {
	// test for duplicatation either by address or by name
	inputName := endpt.Name
	for _, stored := range tf.Endpts {
		if endpt == stored || inputName == stored.Name {
			return
		}
	}
	// add it
	tf.Endpts = append(tf.Endpts, endpt)
}

// AddNetwork adds a Network to the topology configuration (if it is not already present)
func (tf *TopoCfgFrame) AddNetwork(net *NetworkFrame) {
	// test for duplicatation either by address or by name
	inputName := net.Name
	for _, stored := range tf.Networks {
		if net == stored || inputName == stored.Name {
			return
		}
	}
	// add it
	tf.Networks = append(tf.Networks, net)
}

// addRouter adds a Router to the topology configuration (if it is not already present)
func (tf *TopoCfgFrame) addRouter(rtr *RouterFrame) {
	// ignore if router is already present. Comparison by address or by name
	inputName := rtr.Name
	for _, stored := range tf.Routers {
		if rtr == stored || inputName == stored.Name {
			return
		}
	}
	// add it
	tf.Routers = append(tf.Routers, rtr)
}

// addSwitch adds a switch to the topology configuration (if it is not already present)
func (tf *TopoCfgFrame) addSwitch(swtch *SwitchFrame) {
	// ignore if switch is already present. Comparison by address or by name
	inputName := swtch.Name
	for _, stored := range tf.Switches {
		if swtch == stored || inputName == stored.Name {
			return
		}
	}
	// add it
	tf.Switches = append(tf.Switches, swtch)
}

// Consolidate gathers endpts, switches, and routers from the networks added to the TopoCfgFrame,
// and make sure that all the devices referred to in the different components are exposed
// at the TopoCfgFrame level
func (tf *TopoCfgFrame) Consolidate() error {
	if len(tf.Networks) == 0 {
		return fmt.Errorf("no networks given in TopoCfgFrame in Consolidate call")
	}

	tf.Endpts = []*EndptFrame{}
	tf.Routers = []*RouterFrame{}
	tf.Switches = []*SwitchFrame{}

	for _, net := range tf.Networks {

		// ensure the connections between switches and routers in the network
		// net.Consolidate()

		for _, rtr := range net.Routers {
			tf.addRouter(rtr)
		}
		for _, endpt := range net.Endpts {
			tf.addEndpt(endpt)
		}
		for _, swtch := range net.Switches {
			tf.addSwitch(swtch)
		}
	}

	return nil
}

// Transform transforms the slices of pointers to network objects
// into slices of instances of those objects, for serialization
func (tf *TopoCfgFrame) Transform() TopoCfg {
	// first ensure that the TopoCfgFrame is consolidated
	cerr := tf.Consolidate()
	if cerr != nil {
		panic(cerr)
	}

	// create the TopoCfg
	TD := new(TopoCfg)
	TD.Name = tf.Name

	TD.Endpts = make([]EndptDesc, 0)
	for _, endptf := range tf.Endpts {
		endpt := endptf.Transform()
		TD.Endpts = append(TD.Endpts, endpt)
	}

	TD.Networks = make([]NetworkDesc, 0)
	for _, netf := range tf.Networks {
		net := netf.Transform()
		TD.Networks = append(TD.Networks, net)
	}

	TD.Routers = make([]RouterDesc, 0)
	for _, rtrf := range tf.Routers {
		rtr := rtrf.Transform()
		TD.Routers = append(TD.Routers, rtr)
	}

	TD.Switches = make([]SwitchDesc, 0)
	for _, switchf := range tf.Switches {
		swtch := switchf.Transform()
		TD.Switches = append(TD.Switches, swtch)
	}

	return *TD
}

type RtrDescSlice []RouterDesc
type EndptDescSlice []EndptDesc
type NetworkDescSlice []NetworkDesc
type SwitchDescSlice []SwitchDesc
type FlowDescSlice []FlowDesc

// TopoCfg contains all of the networks, routers, and
// endpts, as they are listed in the json file.
type TopoCfg struct {
	Name     string           `json:"name" yaml:"name"`
	Networks NetworkDescSlice `json:"networks" yaml:"networks"`
	Routers  RtrDescSlice     `json:"routers" yaml:"routers"`
	Endpts   EndptDescSlice   `json:"endpts" yaml:"endpts"`
	Switches SwitchDescSlice  `json:"switches" yaml:"switches"`
	Flows    FlowDescSlice    `json:"flows" yaml:"flows"`
}

// A TopoCfgDict holds instances of TopoCfg structures, in a map whose key is
// a name for the topology.  Used to store pre-built instances of networks
type TopoCfgDict struct {
	DictName string             `json:"dictname" yaml:"dictname"`
	Cfgs     map[string]TopoCfg `json:"cfgs" yaml:"cfgs"`
}

// CreateTopoCfgDict is a constructor. Saves the dictionary name, initializes the TopoCfg map.
func CreateTopoCfgDict(name string) *TopoCfgDict {
	tcd := new(TopoCfgDict)
	tcd.DictName = name
	tcd.Cfgs = make(map[string]TopoCfg)

	return tcd
}

// AddTopoCfg includes a TopoCfg into the dictionary, optionally returning an error
// if an TopoCfg with the same name has already been included
func (tcd *TopoCfgDict) AddTopoCfg(tc *TopoCfg, overwrite bool) error {
	if !overwrite {
		_, present := tcd.Cfgs[tc.Name]
		if present {
			return fmt.Errorf("attempt to overwrite TopoCfg %s in TopoCfgDict", tc.Name)
		}
	}

	tcd.Cfgs[tc.Name] = *tc

	return nil
}

// RecoverTopoCfg returns a copy (if one exists) of the TopoCfg with name equal to the input argument name.
// Returns a boolean indicating whether the entry was actually found
func (tcd *TopoCfgDict) RecoverTopoCfg(name string) (*TopoCfg, bool) {
	tc, present := tcd.Cfgs[name]
	if present {
		return &tc, true
	}

	return nil, false
}

// WriteToFile serializes the TopoCfgDict and writes to the file whose name is given as an input argument.
// Extension of the file name selects whether serialization is to json or to yaml format.
func (tcd *TopoCfgDict) WriteToFile(filename string) error {
	pathExt := path.Ext(filename)
	var bytes []byte
	var merr error

	if pathExt == ".yaml" || pathExt == ".YAML" || pathExt == ".yml" {
		bytes, merr = yaml.Marshal(*tcd)
	} else if pathExt == ".json" || pathExt == ".JSON" {
		bytes, merr = json.MarshalIndent(*tcd, "", "\t")
	}

	if merr != nil {
		panic(merr)
	}

	f, cerr := os.Create(filename)
	if cerr != nil {
		panic(cerr)
	}
	_, werr := f.WriteString(string(bytes[:]))
	if werr != nil {
		panic(werr)
	}
	err := f.Close()
	if err != nil {
		panic(err)
	}

	return werr
}

// ReadTopoCfgDict deserializes a slice of bytes into a TopoCfgDict.  If the input arg of bytes
// is empty, the file whose name is given as an argument is read.  Error returned if
// any part of the process generates the error.
func ReadTopoCfgDict(topoCfgDictFileName string, useYAML bool, dict []byte) (*TopoCfgDict, error) {
	var err error

	// read from the file only if the byte slice is empty
	// validate input file name
	if len(dict) == 0 {
		fileInfo, err := os.Stat(topoCfgDictFileName)
		if os.IsNotExist(err) || fileInfo.IsDir() {
			msg := fmt.Sprintf("topology dict %s does not exist or cannot be read", topoCfgDictFileName)
			fmt.Println(msg)

			return nil, errors.New(msg)
		}
		dict, err = os.ReadFile(topoCfgDictFileName)
		if err != nil {
			return nil, err
		}
	}
	example := TopoCfgDict{}

	// extension of input file name indicates whether we are deserializing json or yaml
	if useYAML {
		err = yaml.Unmarshal(dict, &example)
	} else {
		err = json.Unmarshal(dict, &example)
	}
	if err != nil {
		return nil, err
	}

	return &example, nil
}

// BuildTopoCfgFuncType is a signature for a function that creates topology cfgs
type BuildTopoCfgFuncType func(any) *TopoCfg

// BuildExpCfgFuncType is a signature for a function that creates experiment cfgs
type BuildExpCfgFuncType func(any) *ExpCfg

// WriteToFile serializes the TopoCfg and writes to the file whose name is given as an input argument.
// Extension of the file name selects whether serialization is to json or to yaml format.
func (dict *TopoCfg) WriteToFile(filename string) error {
	// path extension of the output file determines whether we serialize to json or to yaml
	pathExt := path.Ext(filename)
	var bytes []byte
	var merr error

	if pathExt == ".yaml" || pathExt == ".YAML" || pathExt == ".yml" {
		bytes, merr = yaml.Marshal(*dict)
	} else if pathExt == ".json" || pathExt == ".JSON" {
		bytes, merr = json.MarshalIndent(*dict, "", "\t")
	}

	if merr != nil {
		panic(merr)
	}

	f, cerr := os.Create(filename)
	if cerr != nil {
		panic(cerr)
	}
	_, werr := f.WriteString(string(bytes[:]))
	if werr != nil {
		panic(werr)
	}
	err := f.Close()
	if err != nil {
		panic(err)
	}

	return werr
}

// ReadTopoCfg deserializes a slice of bytes into a TopoCfg.  If the input arg of bytes
// is empty, the file whose name is given as an argument is read.  Error returned if
// any part of the process generates the error.
func ReadTopoCfg(topoFileName string, useYAML bool, dict []byte) (*TopoCfg, error) {
	var err error

	// read from the file only if the byte slice is empty
	// validate input file name
	if len(dict) == 0 {
		fileInfo, err := os.Stat(topoFileName)
		if os.IsNotExist(err) || fileInfo.IsDir() {
			msg := fmt.Sprintf("topology %s does not exist or cannot be read", topoFileName)
			fmt.Println(msg)

			return nil, errors.New(msg)
		}
		dict, err = os.ReadFile(topoFileName)
		if err != nil {
			return nil, err
		}
	}

	// dict has slice of bytes to process
	example := TopoCfg{}

	// input path extension identifies whether we deserialized encoded json or encoded yaml
	if useYAML {
		err = yaml.Unmarshal(dict, &example)
	} else {
		err = json.Unmarshal(dict, &example)
	}

	if err != nil {
		return nil, err
	}

	return &example, nil
}

// DevDesc provides a description of a computing (or switching or routing) device
type DevDesc struct {
	DevTypes     []string `json:"devtype" yaml:"devtype"`
	Manufacturer string   `json:"manufacturer" yaml:"manufacturer"`
	Model        string   `json:"model" yaml:"model"`
	Cores        int      `json:"cores" yaml:"cores"`
	Freq         float64  `json:"freq" yaml:"freq"`
	Cache        float64  `json:"cache" yaml:"cache"`
}

// DevDescDict defines a dictionary of DevDesc structs, indexed by
// device description string
type DevDescDict struct {
	Name    string             `json:"name" yaml:"name"`
	DescMap map[string]DevDesc `json:"DescMap" yaml:"DescMap"`
}

// CreateDevDescDict is a constructor
func CreateDevDescDict(name string) *DevDescDict {
	ddd := new(DevDescDict)
	ddd.Name = name
	ddd.DescMap = make(map[string]DevDesc)
	return ddd
}

// CreateDevDesc is a constructor
func CreateDevDesc(devType, manufacturer, model string, cores int, freq, cache float64) *DevDesc {
	devTypes := strings.Split(devType, ":")
	dd := DevDesc{DevTypes: devTypes, Manufacturer: manufacturer, Model: model, Cores: cores, Freq: freq, Cache: cache}
	return &dd
}

// AddDevDesc constructs a device identifier by concatenating the Manufacturer and Model
// attributes of the argument device as the index to the referring DevDescDict
func (ddd *DevDescDict) AddDevDesc(dd *DevDesc) {
	// combine the manufacturer and model strings to create the name
	name := dd.Manufacturer + "-" + dd.Model
	ddd.DescMap[name] = *dd
}

// WriteToFile stores the DevDescDict struct to the file whose name is given.
// Serialization to json or to yaml is selected based on the extension of this name.
func (ddd *DevDescDict) WriteToFile(filename string) error {
	pathExt := path.Ext(filename)
	var bytes []byte
	var merr error = nil

	if pathExt == ".yaml" || pathExt == ".YAML" || pathExt == ".yml" {
		bytes, merr = yaml.Marshal(*ddd)
	} else if pathExt == ".json" || pathExt == ".JSON" {
		bytes, merr = json.MarshalIndent(*ddd, "", "\t")
	}

	if merr != nil {
		panic(merr)
	}

	f, cerr := os.Create(filename)
	if cerr != nil {
		panic(cerr)
	}
	_, werr := f.WriteString(string(bytes[:]))
	if werr != nil {
		panic(werr)
	}
	err := f.Close()
	if err != nil {
		panic(err)
	}

	return werr
}

// ReadDevDescDict deserializes a byte slice holding a representation of an DevDescDict struct.
// If the input argument of dict (those bytes) is empty, the file whose name is given is read
// to acquire them.  A deserialized representation is returned, or an error if one is generated
// from a file read or the deserialization.
func ReadDevDescDict(filename string, useYAML bool, dict []byte) (*DevDescDict, error) {
	var err error

	// if the dict slice of bytes is empty we get them from the file whose name is an argument
	if len(dict) == 0 {
		dict, err = os.ReadFile(filename)
		if err != nil {
			return nil, err
		}
	}

	example := DevDescDict{}

	if useYAML {
		err = yaml.Unmarshal(dict, &example)
	} else {
		err = json.Unmarshal(dict, &example)
	}

	if err != nil {
		return nil, err
	}

	return &example, nil
}

func (ddd *DevDescDict) RecoverDevDesc(name string) DevDesc {
	_, present := ddd.DescMap[name]
	if !present {
		panic(fmt.Errorf("device description map %s not found", name))
	}
	return ddd.DescMap[name]
}

// ReportErrs transforms a list of errors and transforms the non-nil ones into a single error
// with comma-separated report of all the constituent errors, and returns it.
func ReportErrs(errs []error) error {
	errMsg := make([]string, 0)
	for _, err := range errs {
		if err != nil {
			errMsg = append(errMsg, err.Error())
		}
	}
	if len(errMsg) == 0 {
		return nil
	}

	return errors.New(strings.Join(errMsg, ","))
}

// CheckDirectories probes the file system for the existence
// of every directory listed in the list of files.  Returns a boolean
// indicating whether all dirs are valid, and returns an aggregated error
// if any checks failed.
func CheckDirectories(dirs []string) (bool, error) {
	// make sure that every directory name included exists
	failures := []string{}

	// for every offered (non-empty) directory
	for _, dir := range dirs {
		if len(dir) == 0 {
			continue
		}

		// look for a extension, if any.   Having one means not a directory
		ext := filepath.Ext(dir)

		// ext being empty means this is a directory, otherwise a path
		if ext != "" {
			failures = append(failures, fmt.Sprintf("%s not a directory", dir))

			continue
		}

		if _, err := os.Stat(dir); err != nil {
			failures = append(failures, fmt.Sprintf("%s not reachable", dir))

			continue
		}
	}
	if len(failures) == 0 {
		return true, nil
	}

	err := errors.New(strings.Join(failures, ","))

	return false, err
}

// CheckReadableFiles probles the file system to ensure that every
// one of the argument filenames exists and is readable
func CheckReadableFiles(names []string) (bool, error) {
	return CheckFiles(names, true)
}

// CheckOutputFiles probles the file system to ensure that every
// argument filename can be written.
func CheckOutputFiles(names []string) (bool, error) {
	return CheckFiles(names, false)
}

// CheckFiles probes the file system for permitted access to all the
// argument filenames, optionally checking also for the existence
// of those files for the purposes of reading them.
func CheckFiles(names []string, checkExistence bool) (bool, error) {
	// make sure that the directory of each named file exists
	errs := make([]error, 0)

	for _, name := range names {

		// skip non-existent files
		if len(name) == 0 || name == "/tmp" {
			continue
		}

		// split off the directory portion of the path
		directory, _ := filepath.Split(name)
		if _, err := os.Stat(directory); err != nil {
			errs = append(errs, err)
		}
	}

	// if required, check for the reachability and existence of each file
	if checkExistence {
		for _, name := range names {
			if _, err := os.Stat(name); err != nil {
				errs = append(errs, err)
			}
		}

		if len(errs) == 0 {
			return true, nil
		}

		rtnerr := ReportErrs(errs)
		return false, rtnerr
	}

	return true, nil
}
