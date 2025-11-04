package mrnes

import (
	"encoding/json"
	"fmt"
	"golang.org/x/exp/slices"
	"gopkg.in/yaml.v3"
	"os"
	"path"
)

// AttrbStruct holds the name of an attribute and a value for it
type AttrbStruct struct {
	AttrbName, AttrbValue string
}

// A valueStruct type holds three different types a value might have,
// typically only one of these is used, and which one is known by context
type valueStruct struct {
	intValue    int
	floatValue  float64
	stringValue string
	boolValue   bool
}

// CreateAttrbStruct is a constructor
func CreateAttrbStruct(attrbName, attrbValue string) *AttrbStruct {
	as := new(AttrbStruct)
	as.AttrbName = attrbName
	as.AttrbValue = attrbValue
	return as
}

// ValidateAttribute checks that the attribute named is one that associates with the parameter object type named
func ValidateAttribute(paramObj, attrbName string) bool {
	_, present := ExpAttributes[paramObj]
	if !present {
		return false
	}

	// wildcard always checks out
	if attrbName == "*" {
		return true
	}

	// result is true if the name is in the list of attributes for the parameter object type
	return slices.Contains(ExpAttributes[paramObj], attrbName)
}

// CompareAttrbs returns -1 if the first argument is strictly more general than the second,
// returns 1 if the second argument is strictly more general than the first, and 0 otherwise
func CompareAttrbs(attrbs1, attrbs2 []AttrbStruct) int {
	// attrbs1 is strictly more general if its length is strictly less and every name it has
	// is shared by attrbs2

	if len(attrbs1) < len(attrbs2) {
		for _, attrb1 := range attrbs1 {
			attrbName := attrb1.AttrbName
			found := false
			for _, attrb2 := range attrbs2 {
				if attrb2.AttrbName == attrbName {
					found = true
					break
				}
			}
			// if attrbName was not found in attrb2 then attrbs1 cannot be strictly more general
			// and (because len(attrbs1) < len(attrbs2)) it cannot be strictly less general
			if !found {
				return 0
			}
		}
		// every attribute name in attrbs1 found in attrbs2, means attrbs1 is more general
		return -1
	}

	if len(attrbs2) < len(attrbs1) {
		for _, attrb2 := range attrbs2 {
			attrbName := attrb2.AttrbName
			found := false
			for _, attrb1 := range attrbs1 {
				if attrb1.AttrbName == attrbName {
					found = true
					break
				}
			}
			// if attrbName was not found in attrb1 then attrbs2 cannot be strictly more general
			// and (because len(attrbs2) < len(attrbs1)) it cannot be strictly less general
			if !found {
				return 0
			}
		}
		// every attribute name in attrbs2 found in attrbs1, means attrbs2 is more general
		return 1
	}
	return 0
}

// EqAttrbs determines whether the two attribute lists are exactly the same
func EqAttrbs(attrbs1, attrbs2 []AttrbStruct) bool {
	if len(attrbs1) != len(attrbs2) {
		return false
	}

	// see whether every attribute in attrbs1 is found in attrbs2
	for _, attrb1 := range attrbs1 {
		found := false
		for _, attrb2 := range attrbs2 {
			if attrb1.AttrbName == attrb2.AttrbName && attrb1.AttrbValue == attrb2.AttrbValue {
				found = true
				break
			}
		}
		// if attrb1.AttrbName not found in attrb2 they can't be equal
		if !found {
			return false
		}
	}

	// now see whether every attribute in attrbs2 is found in attrbs1
	for _, attrb2 := range attrbs2 {
		found := false
		for _, attrb1 := range attrbs1 {
			if attrb2.AttrbName == attrb1.AttrbName && attrb2.AttrbValue == attrb1.AttrbValue {
				found = true
				break
			}
		}
		// if attrb2.AttrbName not found in attrb1 they can't be equal
		if !found {
			return false
		}
	}

	// perfect match among attribute names
	return true
}

// ExpParameter struct describes an input to experiment configuration at run-time. It specifies
//   - ParamObj identifies the kind of thing being configured : Switch, Router, Endpoint, Interface, or Network
//   - Attributes is a list of attributes, each of which are required for the parameter value to be applied.
type ExpParameter struct {
	// Type of thing being configured
	ParamObj string `json:"paramObj" yaml:"paramObj"`

	// attribute identifier for this parameter
	// Attribute string `json:"attribute" yaml:"attribute"`
	Attributes []AttrbStruct `json:"attributes" yaml:"attributes"`

	// ParameterType, e.g., "Bandwidth", "WiredLatency", "model"
	Param string `json:"param" yaml:"param"`

	// string-encoded value associated with type
	Value string `json:"value" yaml:"value"`
}

// Eq returns a boolean flag indicating whether the two ExpParameters referenced in the call are the same
func (epp *ExpParameter) Eq(ep2 *ExpParameter) bool {
	if epp.ParamObj != ep2.ParamObj {
		return false
	}

	if !EqAttrbs(epp.Attributes, ep2.Attributes) {
		return false
	}

	if epp.Param != ep2.Param {
		return false
	}

	if epp.Value != ep2.Value {
		return false
	}
	return true
}

// CreateExpParameter is a constructor.  Completely fills in the struct with the [ExpParameter] attributes.
func CreateExpParameter(paramObj string, attributes []AttrbStruct, param, value string) *ExpParameter {
	exptr := &ExpParameter{ParamObj: paramObj, Attributes: attributes, Param: param, Value: value}

	return exptr
}

// AddAttribute includes another attribute to those associated with the ExpParameter.
// An error is returned if the attribute name (other than 'group') already exists
func (epp *ExpParameter) AddAttribute(attrbName, attrbValue string) error {
	// check whether the attribute name is valid for this parameter
	if !ValidateAttribute(epp.ParamObj, attrbName) {
		return fmt.Errorf("attribute name %s not allowed for parameter object type %s",
			attrbName, epp.ParamObj)
	}

	// check for duplication of attribute as given, and just return if already present
	for _, attrb := range epp.Attributes {
		if attrb.AttrbName == attrbName && attrb.AttrbValue == attrbValue {
			return nil
		}
	}

	// if the attribute name is not 'group', report an attribute name conflict
	if attrbName != "group" {
		for _, attrb := range epp.Attributes {
			if attrb.AttrbName == attrbName {
				return fmt.Errorf("attribute name %s already exists for parameter object", attrbName)
			}
		}
	}

	// create a new AttrbStruct and add it to the list
	epp.Attributes = append(epp.Attributes, *CreateAttrbStruct(attrbName, attrbValue))
	return nil
}

// ExpCfg structure holds all of the ExpParameters for a named experiment
type ExpCfg struct {
	// Name is an identifier for a group of [ExpParameters].  No particular interpretation of this string is
	// used, except as a referencing label when moving an ExpCfg into or out of a dictionary
	Name string `json:"expname" yaml:"expname"`

	// Parameters is a list of all the [ExpParameter] objects presented to the simulator for an experiment.
	Parameters []ExpParameter `json:"parameters" yaml:"parameters"`
}

// AddExpParameter includes the argument ExpParameter to the the Parameter list of the referencing
// ExpCfg
func (excfg *ExpCfg) AddExpParameter(exparam *ExpParameter) {
	excfg.Parameters = append(excfg.Parameters, *exparam)
}

// ExpCfgDict is a dictionary that holds [ExpCfg] objects in a map indexed by their Name.
type ExpCfgDict struct {
	DictName string            `json:"dictname" yaml:"dictname"`
	Cfgs     map[string]ExpCfg `json:"cfgs" yaml:"cfgs"`
}

// CreateExpCfgDict is a constructor.  Saves a name for the dictionary, and initializes the slice of ExpCfg objects
func CreateExpCfgDict(name string) *ExpCfgDict {
	ecd := new(ExpCfgDict)
	ecd.DictName = name
	ecd.Cfgs = make(map[string]ExpCfg)

	return ecd
}

// AddExpCfg adds the offered ExpCfg to the dictionary, optionally returning
// an error if an ExpCfg with the same Name is already saved.
func (ecd *ExpCfgDict) AddExpCfg(ec *ExpCfg, overwrite bool) error {
	// allow for overwriting duplication?
	if !overwrite {
		_, present := ecd.Cfgs[ec.Name]
		if present {
			return fmt.Errorf("attempt to overwrite template ExpCfg %s", ec.Name)
		}
	}
	// save it
	ecd.Cfgs[ec.Name] = *ec

	return nil
}

// RecoverExpCfg returns an ExpCfg from the dictionary, with name equal to the input parameter.
// It returns also a flag denoting whether the identified ExpCfg has an entry in the dictionary.
func (ecd *ExpCfgDict) RecoverExpCfg(name string) (*ExpCfg, bool) {
	ec, present := ecd.Cfgs[name]
	if present {
		return &ec, true
	}

	return nil, false
}

// WriteToFile stores the ExpCfgDict struct to the file whose name is given.
// Serialization to json or to yaml is selected based on the extension of this name.
func (ecd *ExpCfgDict) WriteToFile(filename string) error {
	pathExt := path.Ext(filename)
	var bytes []byte
	var merr error = nil

	if pathExt == ".yaml" || pathExt == ".YAML" || pathExt == ".yml" {
		bytes, merr = yaml.Marshal(*ecd)
	} else if pathExt == ".json" || pathExt == ".JSON" {
		bytes, merr = json.MarshalIndent(*ecd, "", "\t")
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

// ReadExpCfgDict deserializes a byte slice holding a representation of an ExpCfgDict struct.
// If the input argument of dict (those bytes) is empty, the file whose name is given is read
// to acquire them.  A deserialized representation is returned, or an error if one is generated
// from a file read or the deserialization.
func ReadExpCfgDict(filename string, useYAML bool, dict []byte) (*ExpCfgDict, error) {
	var err error
	if len(dict) == 0 {
		dict, err = os.ReadFile(filename)
		if err != nil {
			return nil, err
		}
	}

	example := ExpCfgDict{}
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

// CreateExpCfg is a constructor. Saves the offered Name and initializes the slice of ExpParameters.
func CreateExpCfg(name string) *ExpCfg {
	excfg := &ExpCfg{Name: name, Parameters: make([]ExpParameter, 0)}

	return excfg
}

// ValidateParameter returns an error if the paramObj, attributes, and param values don't
// make sense taken together within an ExpParameter.
func ValidateParameter(paramObj string, attributes []AttrbStruct, param string) error {
	if ExpParamObjs == nil {
		GetExpParamDesc()
	}

	// the paramObj string has to be recognized as one of the permitted ones (stored in list ExpParamObjs)
	if !slices.Contains(ExpParamObjs, paramObj) {
		panic(fmt.Errorf("parameter paramObj %s is not recognized", paramObj))
	}

	// check the validity of each attribute
	for _, attrb := range attributes {
		if !ValidateAttribute(paramObj, attrb.AttrbName) {
			panic(fmt.Errorf("attribute %s not value for parameter object type %s", attrb.AttrbName, paramObj))
		}
	}

	// it's all good
	return nil
}

// AddParameter accepts the four values in an ExpParameter, creates one, and adds to the ExpCfg's list.
// Returns an error if the parameters are not validated.
func (excfg *ExpCfg) AddParameter(paramObj string, attributes []AttrbStruct, param, value string) error {
	// validate the offered parameter values
	err := ValidateParameter(paramObj, attributes, param)
	if err != nil {
		return err
	}

	// create an ExpParameter with these values
	excp := CreateExpParameter(paramObj, attributes, param, value)

	// save it
	excfg.Parameters = append(excfg.Parameters, *excp)
	return nil
}

// WriteToFile stores the ExpCfg struct to the file whose name is given.
// Serialization to json or to yaml is selected based on the extension of this name.
func (excfg *ExpCfg) WriteToFile(filename string) error {
	pathExt := path.Ext(filename)
	var bytes []byte
	var merr error = nil

	if pathExt == ".yaml" || pathExt == ".YAML" || pathExt == ".yml" {
		bytes, merr = yaml.Marshal(*excfg)
	} else if pathExt == ".json" || pathExt == ".JSON" {
		bytes, merr = json.MarshalIndent(*excfg, "", "\t")
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

// ReadExpCfg deserializes a byte slice holding a representation of an ExpCfg struct.
// If the input argument of dict (those bytes) is empty, the file whose name is given is read
// to acquire them.  A deserialized representation is returned, or an error if one is generated
// from a file read or the deserialization.
func ReadExpCfg(filename string, useYAML bool, dict []byte) (*ExpCfg, error) {
	var err error
	if len(dict) == 0 {
		dict, err = os.ReadFile(filename)
		if err != nil {
			return nil, err
		}
	}

	example := ExpCfg{}
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

// UpdateExpCfg copies the ExpCfg parameters in the file referenced by the
// 'updatefile' name into the file of ExpCfg parameters in the file referenced by the
// 'orgfile' name
func UpdateExpCfg(orgfile, updatefile string, useYAML bool, dict []byte) {
	// read in the experiment file
	expCfg, err := ReadExpCfg(orgfile, useYAML, dict)
	if err != nil {
		panic(err)
	}

	// read in the update parameters
	updateCfg, err2 := ReadExpCfg(updatefile, useYAML, []byte{})
	if err2 != nil {
		panic(err2)
	}
	for _, update := range updateCfg.Parameters {
		err := expCfg.AddParameter(update.ParamObj, update.Attributes, update.Param, update.Value)
		if err != nil {
			panic(err)
		}
	}

	// write out the modified configuration
	err = expCfg.WriteToFile(orgfile)
	if err != nil {
		panic(err)
	}
}

// ExpParamObjs , ExpAttributes , and ExpParams hold descriptions of the types of objects
// that are initialized by an exp file, for each the attributes of the object that can be tested for to determine
// whether the object is to receive the configuration parameter, and the parameter types defined for each object type
var ExpParamObjs []string
var ExpAttributes map[string][]string
var ExpParams map[string][]string

// GetExpParamDesc returns ExpParamObjs, ExpAttributes, and ExpParams after ensuring that they have been build
func GetExpParamDesc() ([]string, map[string][]string, map[string][]string) {
	if ExpParamObjs == nil {
		ExpParamObjs = []string{"Switch", "Router", "Endpoint", "Interface", "Network"}
		ExpAttributes = make(map[string][]string)
		ExpAttributes["Switch"] = []string{"name", "group", "model", "*"}
		ExpAttributes["Router"] = []string{"name", "group", "model", "*"}
		ExpAttributes["Endpoint"] = []string{"name", "model", "group", "*"}
		ExpAttributes["Interface"] = []string{"name", "group", "devtype", "devname", "media", "network", "*"}
		ExpAttributes["Network"] = []string{"name", "group", "media", "scale", "*"}
		ExpParams = make(map[string][]string)
		ExpParams["Switch"] = []string{"model", "buffer", "trace"}
		ExpParams["Router"] = []string{"model", "buffer", "trace"}
		ExpParams["Endpoint"] = []string{"trace", "model", "interruptdelay", "bckgrndSrv"}
		ExpParams["Network"] = []string{"latency", "bandwidth", "capacity", "drop", "trace"}
		ExpParams["Interface"] = []string{"latency", "delay", "buffer", "bandwidth",
			"MTU", "rsrvd", "drop", "trace"}
	}

	return ExpParamObjs, ExpAttributes, ExpParams
}
