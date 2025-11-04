"""
Python translation of mrnes/param.go
Contains parameter/configuration parsing and helpers for experiments.
"""
import json
import os
import yaml
from typing import List, Dict, Optional, Tuple


class AttrbStruct:
    """AttrbStruct holds the name of an attribute and a value for it"""

    def __init__(self, AttrbName: str, AttrbValue: str):
        self.AttrbName = AttrbName
        self.AttrbValue = AttrbValue


class valueStruct:
    """A valueStruct type holds three different types a value might have,
    typically only one of these is used, and which one is known by context
    """

    def __init__(self, intValue: int = 0, floatValue: float = 0.0, stringValue: str = "", boolValue: bool = False):
        self.intValue = intValue
        self.floatValue = floatValue
        self.stringValue = stringValue
        self.boolValue = boolValue

# ExpParamObjs , ExpAttributes , and ExpParams hold descriptions of the types of objects
# that are initialized by an exp file, for each the attributes of the object that can be tested for to determine
# whether the object is to receive the configuration parameter, and the parameter types defined for each object type
# ExpAttributes is expected to be populated by GetExpParamDesc()
ExpParamObjs: Optional[List[str]] = None
ExpAttributes: Optional[Dict[str, List[str]]] = None
ExpParams: Optional[Dict[str, List[str]]] = None


def ValidateAttribute(paramObj: str, attrbName: str) -> bool:
    """ValidateAttribute checks that the attribute named is one that associates with the parameter object type named"""
    global ExpAttributes

    if paramObj not in ExpAttributes:
        return False

    # wildcard always checks out
    if attrbName == "*":
        return True

    return attrbName in ExpAttributes[paramObj]


def CompareAttrbs(attrbs1: List[AttrbStruct], attrbs2: List[AttrbStruct]) -> int:
    """CompareAttrbs returns -1 if the first argument is strictly more general than the second,
    returns 1 if the second argument is strictly more general than the first, and 0 otherwise"""
    
    # attrbs1 is strictly more general if its length is strictly less and every name it has
    # is shared by attrbs2
    if len(attrbs1) < len(attrbs2):
        for attrb1 in attrbs1:
            attrbName = attrb1.AttrbName
            found = False
            for attrb2 in attrbs2:
                if attrb2.AttrbName == attrbName:
                    found = True
                    break
            # if attrbName was not found in attrb2 then attrbs1 cannot be strictly more general
            # and (because len(attrbs1) < len(attrbs2)) it cannot be strictly less general
            if not found:
                return 0
        # every attribute name in attrbs1 found in attrbs2, means attrbs1 is more general
        return -1

    if len(attrbs2) < len(attrbs1):
        for attrb2 in attrbs2:
            attrbName = attrb2.AttrbName
            found = False
            for attrb1 in attrbs1:
                if attrb1.AttrbName == attrbName:
                    found = True
                    break
            # if attrbName was not found in attrb1 then attrbs2 cannot be strictly more general
            # and (because len(attrbs2) < len(attrbs1)) it cannot be strictly less general
            if not found:
                return 0
        # every attribute name in attrbs2 found in attrbs1, means attrbs2 is more general
        return 1

    return 0


def EqAttrbs(attrbs1: List[AttrbStruct], attrbs2: List[AttrbStruct]) -> bool:
    """EqAttrbs determines whether the two attribute lists are exactly the same"""
    if len(attrbs1) != len(attrbs2):
        return False

    # see whether every attribute in attrbs1 is found in attrbs2
    for a1 in attrbs1:
        found = False
        for a2 in attrbs2:
            if a1.AttrbName == a2.AttrbName and a1.AttrbValue == a2.AttrbValue:
                found = True
                break
        # if attrb1.AttrbName not found in attrb2 they can't be equal
        if not found:
            return False

    for a2 in attrbs2:
        found = False
        for a1 in attrbs1:
            if a2.AttrbName == a1.AttrbName and a2.AttrbValue == a1.AttrbValue:
                found = True
                break
        # if attrb2.AttrbName not found in attrb1 they can't be equal
        if not found:
            return False

    return True


class ExpParameter:
    """ExpParameter struct describes an input to experiment configuration at run-time. It specifies
    - ParamObj identifies the kind of thing being configured : Switch, Router, Endpoint, Interface, or Network
    - Attributes is a list of attributes, each of which are required for the parameter value to be applied.
    """

    def __init__(self, ParamObj: str, Attributes: List[AttrbStruct], Param: str, Value: str):
        # Type of thing being configured
        self.ParamObj = ParamObj
        # attribute identifier for this parameter
        self.Attributes = Attributes
        # ParameterType, e.g., "Bandwidth", "WiredLatency", "model"
        self.Param = Param
        # string-encoded value associated with type
        self.Value = Value

    def Eq(self, ep2: "ExpParameter") -> bool:
        """Eq returns a boolean flag indicating whether the two ExpParameters referenced in the call are the same"""
        if self.ParamObj != ep2.ParamObj:
            return False
        
        if not EqAttrbs(self.Attributes, ep2.Attributes):
            return False
        
        if self.Param != ep2.Param:
            return False
        
        if self.Value != ep2.Value:
            return False
        
        return True

    def AddAttribute(self, attrbName: str, attrbValue: str) -> Optional[Exception]:
        """AddAttribute includes another attribute to those associated with the ExpParameter.
        Returns an Exception object on error (matching other methods that return Optional[Exception]).
        """
        # check whether the attribute name is valid for this parameter
        if not ValidateAttribute(self.ParamObj, attrbName):
            return Exception(f"attribute name {attrbName} not allowed for parameter object type {self.ParamObj}")

        # check for duplication of attribute as given, and just return if already present
        for attrb in self.Attributes:
            if attrb.AttrbName == attrbName and attrb.AttrbValue == attrbValue:
                return None

        # if the attribute name is not 'group', report an attribute name conflict
        if attrbName != "group":
            for attrb in self.Attributes:
                if attrb.AttrbName == attrbName:
                    return Exception(f"attribute name {attrbName} already exists for parameter object")

        # create a new AttrbStruct and add it to the list
        self.Attributes.append(AttrbStruct(attrbName, attrbValue))

        return None


class ExpCfg:
    """ExpCfg structure holds all of the ExpParameters for a named experiment"""

    def __init__(self, Name: str):
        # Name is an identifier for a group of ExpParameters.  No particular interpretation of this string is
	    # used, except as a referencing label when moving an ExpCfg into or out of a dictionary
        self.Name = Name
        # Parameters is a list of all the ExpParameter objects
        self.Parameters: List[ExpParameter] = []

    def AddExpParameter(self, exparam: ExpParameter):
        """AddExpParameter includes the argument ExpParameter to the the Parameter list of the referencing ExpCfg"""
        self.Parameters.append(exparam)

    def AddParameter(self, paramObj: str, attributes: List[AttrbStruct], param: str, value: str) -> Optional[Exception]:
        """AddParameter accepts the four values in an ExpParameter, creates one, and adds to the ExpCfg's list.
        Returns an error if the parameters are not validated."""
        err = ValidateParameter(paramObj, attributes, param)
        if err is not None:
            return err

        # create the ExpParameter with these values
        excp = ExpParameter(paramObj, attributes, param, value)

        # save it
        self.Parameters.append(excp)
        return None

    def WriteToFile(self, filename: str) -> Optional[Exception]:
        """WriteToFile stores the ExpCfg struct to the file whose name is given.
        Serialization to json or to yaml is selected based on the extension of this name.
        """
        pathExt = os.path.splitext(filename)[1].lower()
        
        try:
            if pathExt in (".yaml", ".yml"):
                with open(filename, "w") as f:
                    yaml.dump(self.__dict__, f)
            elif pathExt == ".json":
                with open(filename, "w") as f:
                    json.dump(self.__dict__, f, indent=2)
        except Exception as e:
            raise Exception(f"Error writing ExpCfg to file {filename}: {e}")
        
        return None


class ExpCfgDict:
    """ExpCfgDict is a dictionary that holds ExpCfg objects in a map indexed by their Name."""

    def __init__(self, DictName: str):
        self.DictName = DictName
        self.Cfgs: Dict[str, ExpCfg] = {}

    def AddExpCfg(self, ec: ExpCfg, overwrite: bool = False) -> Optional[Exception]:
        """AddExpCfg adds the offered ExpCfg to the dictionary, optionally returning
        an error if an ExpCfg with the same Name is already saved.
        """
        # only overwrite if requested
        if not overwrite and ec.Name in self.Cfgs:
            return Exception(f"attempt to overwrite template ExpCfg {ec.Name}")
        
        # save it
        self.Cfgs[ec.Name] = ec
        
        return None

    def RecoverExpCfg(self, name: str) -> Tuple[Optional[ExpCfg], bool]:
        """RecoverExpCfg returns an ExpCfg from the dictionary, with name equal to the input parameter.
        It also returns a flag denoting whether the identified ExpCfg has an entry in the dictionary."""
        if name in self.Cfgs:
            return self.Cfgs[name], True
        
        return None, False

    def WriteToFile(self, filename: str) -> Optional[Exception]:
        """WriteToFile stores the ExpCfgDict struct to the file whose name is given.
        Serialization to json or to yaml is selected based on the extension of this name.
        """
        pathExt = os.path.splitext(filename)[1].lower()

        try:
            if pathExt in (".yaml", ".yml"):
                with open(filename, "w") as f:
                    yaml.dump({"dictname": self.DictName, "cfgs": {k: v.__dict__ for k, v in self.Cfgs.items()}}, f)
            elif pathExt == ".json":
                with open(filename, "w") as f:
                    json.dump({"dictname": self.DictName, "cfgs": {k: v.__dict__ for k, v in self.Cfgs.items()}}, f, indent=2)
        except Exception as e:
            raise Exception(f"Error writing ExpCfgDict to file {filename}: {e}")

        return None


def ReadExpCfgDict(filename: str, useYAML: bool, dict_bytes: bytes) -> Tuple[Optional[ExpCfgDict], Optional[Exception]]:
    """ReadExpCfgDict deserializes a byte slice holding a representation of an ExpCfgDict struct.
    If the input argument of dict (those bytes) is empty, the file whose name is given is read
    to acquire them. A deserialized representation is returned, or an error if one is generated
    from a file read or the deserialization.
    """
    try:
        if not dict_bytes:
            dict_bytes = open(filename, "rb").read()
    
    except Exception as e:
        return None, e

    try:
        if useYAML:
            obj = yaml.safe_load(dict_bytes)
        else:
            obj = json.loads(dict_bytes)
    
    except Exception as e:
        return None, e

    # Reconstruct ExpCfgDict
    ecd = ExpCfgDict(obj.get("dictname", ""))
    cfgs = obj.get("cfgs", {})
    
    for name, v in cfgs.items():
        ec = ExpCfg(name)
        params = v.get("parameters") or v.get("Parameters") or []
        for p in params:
            attrs = []
            for a in p.get("attributes", []) or p.get("Attributes", []):
                attrs.append(AttrbStruct(a.get("AttrbName", a.get("attrbName", "")), a.get("AttrbValue", a.get("attrbValue", ""))))
            
            ep = ExpParameter(p.get("paramObj", p.get("ParamObj", "")), attrs, p.get("param", p.get("Param", "")), p.get("value", p.get("Value", "")))
            ec.Parameters.append(ep)
        ecd.Cfgs[name] = ec

    return ecd, None


def ReadExpCfg(filename: str, useYAML: bool, dict_bytes: bytes) -> Tuple[Optional[ExpCfg], Optional[Exception]]:
    """ReadExpCfg deserializes a byte slice holding a representation of an ExpCfg struct."""
    try:
        if not dict_bytes:
            dict_bytes = open(filename, "rb").read()
    except Exception as e:
        return None, e

    try:
        if useYAML:
            obj = yaml.safe_load(dict_bytes)
        else:
            obj = json.loads(dict_bytes)
    
    except Exception as e:
        return None, e

    # Reconstruct ExpCfg
    ec = ExpCfg(obj.get("expname", obj.get("Name", "")))
    params = obj.get("parameters", [])
    for p in params:
        attrs = []
        for a in p.get("attributes", []) or p.get("Attributes", []):
            attrs.append(AttrbStruct(a.get("AttrbName", a.get("attrbName", "")), a.get("AttrbValue", a.get("attrbValue", ""))))
        ep = ExpParameter(p.get("paramObj", p.get("ParamObj", "")), attrs, p.get("param", p.get("Param", "")), p.get("value", p.get("Value", "")))
        ec.Parameters.append(ep)
    
    return ec, None


def UpdateExpCfg(orgfile: str, updatefile: str, useYAML: bool, dict_bytes: bytes) -> None:
    """UpdateExpCfg copies the ExpCfg parameters in the file referenced by the
    'updatefile' name into the file of ExpCfg parameters in the file referenced by the
    'orgfile' name
    """
    # read in the experiment file
    expCfg, err = ReadExpCfg(orgfile, useYAML, dict_bytes)
    if err is not None:
        raise err

    # read in the update parameters
    updateCfg, err2 = ReadExpCfg(updatefile, useYAML, b"")
    if err2 is not None:
        raise err2

    # apply the updates
    for update in updateCfg.Parameters:
        e = expCfg.AddParameter(update.ParamObj, update.Attributes, update.Param, update.Value)
        if e is not None:
            raise e

    # write out the modified configuration
    expCfg.WriteToFile(orgfile)


def ValidateParameter(paramObj: str, attributes: List[AttrbStruct], param: str) -> Optional[Exception]:
    """ValidateParameter returns an error if the paramObj, attributes, and param values don't
    make sense taken together within an ExpParameter.
    """
    global ExpParamObjs, ExpAttributes, ExpParams
    if ExpParamObjs is None:
        GetExpParamDesc()

    if paramObj not in ExpParamObjs:
        raise Exception(f"parameter paramObj {paramObj} is not recognized")

    for attrb in attributes:
        if not ValidateAttribute(paramObj, attrb.AttrbName):
            raise Exception(f"attribute {attrb.AttrbName} not value for parameter object type {paramObj}")

    # all good
    return None


def GetExpParamDesc() -> Tuple[List[str], Dict[str, List[str]], Dict[str, List[str]]]:
    """GetExpParamDesc returns ExpParamObjs, ExpAttributes, and ExpParams after ensuring that they have been build"""
    global ExpParamObjs, ExpAttributes, ExpParams
    if ExpParamObjs is None:
        ExpParamObjs = ["Switch", "Router", "Endpoint", "Interface", "Network"]
        ExpAttributes = {}
        ExpAttributes["Switch"] = ["name", "group", "model", "*"]
        ExpAttributes["Router"] = ["name", "group", "model", "*"]
        ExpAttributes["Endpoint"] = ["name", "model", "group", "*"]
        ExpAttributes["Interface"] = ["name", "group", "devtype", "devname", "media", "network", "*"]
        ExpAttributes["Network"] = ["name", "group", "media", "scale", "*"]
        ExpParams = {}
        ExpParams["Switch"] = ["model", "buffer", "trace"]
        ExpParams["Router"] = ["model", "buffer", "trace"]
        ExpParams["Endpoint"] = ["trace", "model", "interruptdelay", "bckgrndSrv"]
        ExpParams["Network"] = ["latency", "bandwidth", "capacity", "drop", "trace"]
        ExpParams["Interface"] = ["latency", "delay", "buffer", "bandwidth", "MTU", "rsrvd", "drop", "trace"]
    return ExpParamObjs, ExpAttributes, ExpParams
